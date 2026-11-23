"""
Classroom Allocation System  -  Software Engineering Project
Flask + SQLite web application.
Run:  python app.py      then open  http://127.0.0.1:5000
"""
import os
import csv
import io
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, g, render_template, request, redirect, url_for,
                   session, flash, abort, Response)
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CAS_DB", os.path.join(BASE, "classroom.db"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-in-production")

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
PERIODS = [("09:00", "10:00"), ("10:00", "11:00"), ("11:15", "12:15"),
           ("12:15", "13:15"), ("14:00", "15:00"), ("15:00", "16:00")]
ROOM_TYPES = ["Classroom", "Laboratory", "Seminar Hall"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_login(
  user_id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','faculty','student')),
  display_name TEXT NOT NULL,
  batch TEXT
);
CREATE TABLE IF NOT EXISTS faculty(
  facultyId INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER UNIQUE REFERENCES user_login(user_id) ON DELETE CASCADE,
  name TEXT NOT NULL, email TEXT, department TEXT, phone TEXT
);
CREATE TABLE IF NOT EXISTS course(
  courseId INTEGER PRIMARY KEY AUTOINCREMENT,
  courseCode TEXT UNIQUE NOT NULL, courseName TEXT NOT NULL,
  department TEXT, credits INTEGER DEFAULT 3
);
CREATE TABLE IF NOT EXISTS classroom(
  roomId INTEGER PRIMARY KEY AUTOINCREMENT,
  roomNo TEXT UNIQUE NOT NULL, building TEXT, roomType TEXT DEFAULT 'Classroom',
  capacity INTEGER NOT NULL, hasProjector INTEGER DEFAULT 0,
  status TEXT DEFAULT 'Available'
);
CREATE TABLE IF NOT EXISTS time_slot(
  slotId INTEGER PRIMARY KEY AUTOINCREMENT,
  dayOfWeek TEXT NOT NULL, startTime TEXT NOT NULL, endTime TEXT NOT NULL,
  UNIQUE(dayOfWeek, startTime, endTime)
);
CREATE TABLE IF NOT EXISTS allocation_request(
  requestId INTEGER PRIMARY KEY AUTOINCREMENT,
  facultyId INTEGER NOT NULL REFERENCES faculty(facultyId),
  courseId INTEGER NOT NULL REFERENCES course(courseId),
  slotId INTEGER NOT NULL REFERENCES time_slot(slotId),
  reqDate TEXT NOT NULL, capacityNeeded INTEGER NOT NULL,
  needProjector INTEGER DEFAULT 0, roomType TEXT DEFAULT 'Any',
  status TEXT DEFAULT 'Pending', reason TEXT,
  createdOn TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS allocation(
  allocationId INTEGER PRIMARY KEY AUTOINCREMENT,
  requestId INTEGER UNIQUE REFERENCES allocation_request(requestId),
  roomId INTEGER NOT NULL REFERENCES classroom(roomId),
  slotId INTEGER NOT NULL REFERENCES time_slot(slotId),
  reqDate TEXT NOT NULL, status TEXT DEFAULT 'Confirmed'
);
CREATE TABLE IF NOT EXISTS notification(
  notifId INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES user_login(user_id) ON DELETE CASCADE,
  allocationId INTEGER, message TEXT NOT NULL,
  sentOn TEXT DEFAULT CURRENT_TIMESTAMP, isRead INTEGER DEFAULT 0
);
"""

# ------------------------------------------------------------------ database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def q(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def ex(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


# --------------------------------------------------------------------- auth
def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if "uid" not in session:
            flash("Please log in first.", "warn")
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


def role_required(*roles):
    def deco(f):
        @wraps(f)
        @login_required
        def wrapper(*a, **kw):
            if session.get("role") not in roles:
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco


def current_faculty_id():
    r = q("SELECT facultyId FROM faculty WHERE user_id=?", (session["uid"],), one=True)
    return r["facultyId"] if r else None


@app.context_processor
def inject_globals():
    unread = 0
    if "uid" in session:
        unread = q("SELECT COUNT(*) c FROM notification WHERE user_id=? AND isRead=0",
                   (session["uid"],), one=True)["c"]
    return dict(unread=unread, DAYS=DAYS, today=date.today().isoformat())


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", code=403, msg="You do not have permission to open this page."), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, msg="Page not found."), 404


# ---------------------------------------------------------- allocation logic
def notify(user_id, message, allocation_id=None):
    ex("INSERT INTO notification(user_id, allocationId, message) VALUES(?,?,?)",
       (user_id, allocation_id, message))


def slot_label(s):
    return f"{s['dayOfWeek'][:3]} {s['startTime']}-{s['endTime']}"


def free_rooms(date_s, slot_id, need, projector, rtype):
    """Rooms that satisfy the request and are free in that date/slot, best fit first."""
    return q("""
        SELECT * FROM classroom c
        WHERE c.status='Available' AND c.capacity >= ?
          AND (? = 0 OR c.hasProjector = 1)
          AND (? = 'Any' OR c.roomType = ?)
          AND c.roomId NOT IN (SELECT roomId FROM allocation
                               WHERE reqDate=? AND slotId=? AND status='Confirmed')
        ORDER BY c.capacity ASC, c.roomNo ASC""",
             (need, projector, rtype, rtype, date_s, slot_id))


def faculty_busy(faculty_id, date_s, slot_id, ignore_request=None):
    r = q("""SELECT COUNT(*) c FROM allocation a JOIN allocation_request r ON r.requestId=a.requestId
             WHERE r.facultyId=? AND a.reqDate=? AND a.slotId=? AND a.status='Confirmed'
               AND r.requestId != ?""",
          (faculty_id, date_s, slot_id, ignore_request or -1), one=True)
    return r["c"] > 0


def suggest_alternatives(req, limit=3):
    """Other free slots on the same date that have a suitable room."""
    slot = q("SELECT * FROM time_slot WHERE slotId=?", (req["slotId"],), one=True)
    out = []
    for s in q("SELECT * FROM time_slot WHERE dayOfWeek=? AND slotId!=? ORDER BY startTime",
               (slot["dayOfWeek"], slot["slotId"])):
        rooms = free_rooms(req["reqDate"], s["slotId"], req["capacityNeeded"],
                           req["needProjector"], req["roomType"])
        if rooms and not faculty_busy(req["facultyId"], req["reqDate"], s["slotId"], req["requestId"]):
            out.append(f"{s['startTime']}-{s['endTime']} (Room {rooms[0]['roomNo']})")
        if len(out) >= limit:
            break
    return out


def process_request(request_id):
    """Allocation engine: validate -> conflict check -> best-fit room -> notify."""
    req = q("""SELECT r.*, f.user_id AS fuser, c.courseCode FROM allocation_request r
               JOIN faculty f ON f.facultyId=r.facultyId
               JOIN course c ON c.courseId=r.courseId WHERE r.requestId=?""",
            (request_id,), one=True)
    slot = q("SELECT * FROM time_slot WHERE slotId=?", (req["slotId"],), one=True)

    if faculty_busy(req["facultyId"], req["reqDate"], req["slotId"], request_id):
        reason = "You already have another class booked in this time slot."
        ex("UPDATE allocation_request SET status='Rejected', reason=? WHERE requestId=?", (reason, request_id))
        notify(req["fuser"], f"Request #{request_id} ({req['courseCode']}) rejected: {reason}")
        return "Rejected", reason

    rooms = free_rooms(req["reqDate"], req["slotId"], req["capacityNeeded"],
                       req["needProjector"], req["roomType"])
    if rooms:
        room = rooms[0]  # smallest suitable room = best fit
        aid = ex("""INSERT INTO allocation(requestId, roomId, slotId, reqDate, status)
                    VALUES(?,?,?,?, 'Confirmed')""",
                 (request_id, room["roomId"], req["slotId"], req["reqDate"]))
        ex("UPDATE allocation_request SET status='Allocated', reason=NULL WHERE requestId=?", (request_id,))
        notify(req["fuser"],
               f"Room {room['roomNo']} ({room['building']}) allocated for {req['courseCode']} "
               f"on {req['reqDate']}, {slot['startTime']}-{slot['endTime']}.", aid)
        return "Allocated", f"Room {room['roomNo']} allocated."

    alts = suggest_alternatives(req)
    reason = "No suitable free room for this date and slot."
    if alts:
        reason += " Try: " + "; ".join(alts) + "."
    ex("UPDATE allocation_request SET status='Rejected', reason=? WHERE requestId=?", (reason, request_id))
    notify(req["fuser"], f"Request #{request_id} ({req['courseCode']}) could not be allocated. {reason}")
    return "Rejected", reason


def cancel_request(request_id, by_admin=False, note=""):
    req = q("""SELECT r.*, f.user_id AS fuser, c.courseCode FROM allocation_request r
               JOIN faculty f ON f.facultyId=r.facultyId JOIN course c ON c.courseId=r.courseId
               WHERE r.requestId=?""", (request_id,), one=True)
    ex("UPDATE allocation SET status='Released' WHERE requestId=? AND status='Confirmed'", (request_id,))
    ex("UPDATE allocation_request SET status='Cancelled', reason=? WHERE requestId=?",
       (note or ("Cancelled by administrator" if by_admin else "Cancelled by faculty"), request_id))
    who = "the administrator" if by_admin else "you"
    notify(req["fuser"], f"Booking for {req['courseCode']} on {req['reqDate']} was cancelled by {who}. {note}".strip())


def reallocate_room_bookings(room_id):
    """When a room goes under maintenance, try to move its future bookings to other rooms."""
    moved, failed = 0, 0
    rows = q("""SELECT a.allocationId, a.requestId FROM allocation a
                WHERE a.roomId=? AND a.status='Confirmed' AND a.reqDate>=?""",
             (room_id, date.today().isoformat()))
    for r in rows:
        ex("UPDATE allocation SET status='Released' WHERE allocationId=?", (r["allocationId"],))
        ex("DELETE FROM allocation WHERE requestId=? AND status='Released'", (r["requestId"],))
        st, _ = process_request(r["requestId"])
        if st == "Allocated":
            moved += 1
        else:
            failed += 1
    return moved, failed


def week_start(d):
    return d - timedelta(days=d.weekday())


def parse_date(s, default=None):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------- routes
@app.route("/")
def index():
    return redirect(url_for("dashboard" if "uid" in session else "login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM user_login WHERE username=? COLLATE NOCASE", (request.form["username"].strip(),), one=True)
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            session.clear()
            session.update(uid=u["user_id"], role=u["role"], name=u["display_name"])
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "err")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "ok")
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        username, name, role = f["username"].strip(), f["name"].strip(), f["role"]
        if role not in ("faculty", "student") or not username or not name or len(f["password"]) < 6:
            flash("Fill all fields; password must have at least 6 characters.", "err")
            return render_template("register.html")
        if q("SELECT 1 FROM user_login WHERE username=?", (username,), one=True):
            flash("Username already taken.", "err")
            return render_template("register.html")
        uid = ex("INSERT INTO user_login(username,password_hash,role,display_name,batch) VALUES(?,?,?,?,?)",
                 (username, generate_password_hash(f["password"]), role, name, f.get("batch", "").strip() or None))
        if role == "faculty":
            ex("INSERT INTO faculty(user_id,name,email,department,phone) VALUES(?,?,?,?,?)",
               (uid, name, f.get("email", ""), f.get("department", ""), f.get("phone", "")))
        flash("Account created. Please log in.", "ok")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/dashboard")
@login_required
def dashboard():
    role, today_s = session["role"], date.today().isoformat()
    ctx = {}
    now = datetime.now()
    cur = None
    if now.weekday() < 6:
        hm = now.strftime("%H:%M")
        for s_, e_ in PERIODS:
            if s_ <= hm < e_:
                cur = (s_, e_)
    busy_now = {}
    if cur:
        for r in q("""SELECT a.roomId, c.courseCode, f.name fname FROM allocation a
                      JOIN allocation_request r USING(requestId) JOIN course c ON c.courseId=r.courseId
                      JOIN faculty f ON f.facultyId=r.facultyId JOIN time_slot s ON s.slotId=a.slotId
                      WHERE a.reqDate=? AND a.status='Confirmed' AND s.startTime=?""", (today_s, cur[0])):
            busy_now[r["roomId"]] = f"{r['courseCode']} · {r['fname']}"
    live = []
    for rm in q("SELECT * FROM classroom ORDER BY roomNo"):
        state = "Maintenance" if rm["status"] != "Available" else ("Occupied" if rm["roomId"] in busy_now else "Free")
        live.append(dict(room=rm, state=state, info=busy_now.get(rm["roomId"], "")))
    ctx["live"], ctx["cur_slot"] = live, cur
    if role == "admin":
        ctx["stats"] = dict(
            rooms=q("SELECT COUNT(*) c FROM classroom", one=True)["c"],
            available=q("SELECT COUNT(*) c FROM classroom WHERE status='Available'", one=True)["c"],
            faculty=q("SELECT COUNT(*) c FROM faculty", one=True)["c"],
            today=q("SELECT COUNT(*) c FROM allocation WHERE reqDate=? AND status='Confirmed'", (today_s,), one=True)["c"],
            allocated=q("SELECT COUNT(*) c FROM allocation_request WHERE status='Allocated'", one=True)["c"],
            rejected=q("SELECT COUNT(*) c FROM allocation_request WHERE status='Rejected'", one=True)["c"])
        mon = week_start(date.today())
        wk = {r["reqDate"]: r["n"] for r in q(
            "SELECT reqDate, COUNT(*) n FROM allocation WHERE status='Confirmed' AND reqDate BETWEEN ? AND ? GROUP BY reqDate",
            (mon.isoformat(), (mon + timedelta(days=5)).isoformat()))}
        ctx["week"] = [(DAYS[i][:3], wk.get((mon + timedelta(days=i)).isoformat(), 0)) for i in range(6)]
        ctx["weekmax"] = max([n for _, n in ctx["week"]] + [1])
        rs = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM allocation_request GROUP BY status")}
        tot = max(sum(rs.values()), 1)
        a_ = round(rs.get("Allocated", 0) * 100 / tot)
        r_ = round(rs.get("Rejected", 0) * 100 / tot)
        ctx["donut"] = dict(a=a_, ar=a_ + r_, total=sum(rs.values()), allocated=rs.get("Allocated", 0),
                            rejected=rs.get("Rejected", 0), cancelled=rs.get("Cancelled", 0))
        avail = max(ctx["stats"]["available"], 1)
        ctx["occupancy"] = min(100, round(ctx["stats"]["today"] * 100 / (avail * len(PERIODS))))
        ctx["recent"] = q("""SELECT r.*, c.courseCode, f.name fname, s.startTime, s.endTime FROM allocation_request r
                             JOIN course c USING(courseId) JOIN faculty f USING(facultyId) JOIN time_slot s USING(slotId)
                             ORDER BY r.requestId DESC LIMIT 6""")
    elif role == "faculty":
        fid = current_faculty_id()
        ctx["upcoming"] = q("""SELECT a.reqDate, s.startTime, s.endTime, cl.roomNo, cl.building, c.courseCode, c.courseName
                               FROM allocation a JOIN allocation_request r USING(requestId)
                               JOIN time_slot s ON s.slotId=a.slotId JOIN classroom cl USING(roomId)
                               JOIN course c ON c.courseId=r.courseId
                               WHERE r.facultyId=? AND a.status='Confirmed' AND a.reqDate>=?
                               ORDER BY a.reqDate, s.startTime LIMIT 8""", (fid, today_s))
        ctx["counts"] = {r["status"]: r["n"] for r in q(
            "SELECT status, COUNT(*) n FROM allocation_request WHERE facultyId=? GROUP BY status", (fid,))}
    else:
        ctx["classes"] = q("""SELECT s.startTime, s.endTime, cl.roomNo, cl.building, c.courseCode, c.courseName, f.name fname
                              FROM allocation a JOIN allocation_request r USING(requestId)
                              JOIN time_slot s ON s.slotId=a.slotId JOIN classroom cl USING(roomId)
                              JOIN course c ON c.courseId=r.courseId JOIN faculty f ON f.facultyId=r.facultyId
                              WHERE a.reqDate=? AND a.status='Confirmed' ORDER BY s.startTime""", (today_s,))
    return render_template("dashboard.html", **ctx)


# ---- faculty: request / my requests
@app.route("/request/new", methods=["GET", "POST"])
@role_required("faculty")
def new_request():
    courses = q("SELECT * FROM course ORDER BY courseCode")
    slots = q("SELECT * FROM time_slot ORDER BY startTime")
    if request.method == "POST":
        f = request.form
        d = parse_date(f.get("reqDate"))
        slot = q("SELECT * FROM time_slot WHERE slotId=?", (f.get("slotId"),), one=True)
        try:
            need = int(f.get("capacityNeeded", 0))
        except ValueError:
            need = 0
        err = None
        if not d or d < date.today():
            err = "Choose today or a future date."
        elif not slot or DAYS[d.weekday()] != slot["dayOfWeek"]:
            err = "The chosen time slot does not fall on the weekday of the chosen date."
        elif need < 1 or need > 1000:
            err = "Enter a valid class strength."
        elif not q("SELECT 1 FROM course WHERE courseId=?", (f.get("courseId"),), one=True):
            err = "Select a course."
        if err:
            flash(err, "err")
            return render_template("request_form.html", courses=courses, slots=slots, ROOM_TYPES=ROOM_TYPES, form=f)
        rid = ex("""INSERT INTO allocation_request(facultyId,courseId,slotId,reqDate,capacityNeeded,needProjector,roomType)
                    VALUES(?,?,?,?,?,?,?)""",
                 (current_faculty_id(), f["courseId"], slot["slotId"], d.isoformat(), need,
                  1 if f.get("needProjector") else 0, f.get("roomType", "Any")))
        status, msg = process_request(rid)
        flash(msg, "ok" if status == "Allocated" else "err")
        return redirect(url_for("my_requests"))
    return render_template("request_form.html", courses=courses, slots=slots, ROOM_TYPES=ROOM_TYPES, form={})


@app.route("/requests/mine")
@role_required("faculty")
def my_requests():
    rows = q("""SELECT r.*, c.courseCode, c.courseName, s.startTime, s.endTime, cl.roomNo, cl.building
                FROM allocation_request r JOIN course c USING(courseId) JOIN time_slot s USING(slotId)
                LEFT JOIN allocation a ON a.requestId=r.requestId AND a.status='Confirmed'
                LEFT JOIN classroom cl ON cl.roomId=a.roomId
                WHERE r.facultyId=? ORDER BY r.requestId DESC""", (current_faculty_id(),))
    return render_template("my_requests.html", rows=rows)


@app.route("/requests/<int:rid>/cancel", methods=["POST"])
@role_required("faculty", "admin")
def cancel(rid):
    r = q("SELECT * FROM allocation_request WHERE requestId=?", (rid,), one=True)
    if not r:
        abort(404)
    admin = session["role"] == "admin"
    if not admin and r["facultyId"] != current_faculty_id():
        abort(403)
    if r["status"] != "Allocated":
        flash("Only allocated bookings can be cancelled.", "warn")
    else:
        cancel_request(rid, by_admin=admin, note=request.form.get("note", ""))
        flash("Booking cancelled and room released.", "ok")
    return redirect(url_for("admin_requests" if admin else "my_requests"))


# ---- everyone: availability + timetable + notifications
@app.route("/availability")
@login_required
def availability():
    d = parse_date(request.args.get("date"), date.today())
    day = DAYS[d.weekday()] if d.weekday() < 6 else None
    slots = q("SELECT * FROM time_slot WHERE dayOfWeek=? ORDER BY startTime", (day,)) if day else []
    rooms = q("SELECT * FROM classroom ORDER BY building, roomNo")
    booked = {}
    for r in q("""SELECT a.roomId, a.slotId, c.courseCode, f.name fname FROM allocation a
                  JOIN allocation_request r USING(requestId) JOIN course c ON c.courseId=r.courseId
                  JOIN faculty f ON f.facultyId=r.facultyId
                  WHERE a.reqDate=? AND a.status='Confirmed'""", (d.isoformat(),)):
        booked[(r["roomId"], r["slotId"])] = f"{r['courseCode']} - {r['fname']}"
    return render_template("availability.html", d=d, day=day, slots=slots, rooms=rooms, booked=booked)


@app.route("/timetable")
@login_required
def timetable():
    d = parse_date(request.args.get("date"), date.today())
    mon = week_start(d)
    days = [mon + timedelta(days=i) for i in range(6)]
    room_f = request.args.get("room", "")
    mine = request.args.get("mine") == "1" and session["role"] == "faculty"
    sql = """SELECT a.reqDate, a.slotId, cl.roomNo, c.courseCode, c.courseName, f.name fname
             FROM allocation a JOIN allocation_request r USING(requestId)
             JOIN classroom cl USING(roomId) JOIN course c ON c.courseId=r.courseId
             JOIN faculty f ON f.facultyId=r.facultyId
             WHERE a.status='Confirmed' AND a.reqDate BETWEEN ? AND ?"""
    args = [days[0].isoformat(), days[-1].isoformat()]
    if room_f:
        sql += " AND a.roomId=?"
        args.append(room_f)
    if mine:
        sql += " AND r.facultyId=?"
        args.append(current_faculty_id())
    cells = {}
    for r in q(sql, args):
        cells.setdefault((r["reqDate"], r["slotId"]), []).append(r)
    slot_ids = {(s["dayOfWeek"], s["startTime"], s["endTime"]): s["slotId"] for s in q("SELECT * FROM time_slot")}
    return render_template("timetable.html", d=d, days=days, periods=PERIODS, cells=cells, slot_ids=slot_ids,
                           rooms=q("SELECT * FROM classroom ORDER BY roomNo"), room_f=room_f, mine=mine,
                           prev=(mon - timedelta(days=7)).isoformat(), nxt=(mon + timedelta(days=7)).isoformat())


@app.route("/notifications")
@login_required
def notifications():
    rows = q("SELECT * FROM notification WHERE user_id=? ORDER BY notifId DESC LIMIT 100", (session["uid"],))
    ex("UPDATE notification SET isRead=1 WHERE user_id=?", (session["uid"],))
    return render_template("notifications.html", rows=rows)


# ---- admin
@app.route("/admin/rooms", methods=["GET", "POST"])
@role_required("admin")
def admin_rooms():
    if request.method == "POST":
        f = request.form
        try:
            cap = int(f["capacity"])
            assert cap > 0
            ex("INSERT INTO classroom(roomNo,building,roomType,capacity,hasProjector) VALUES(?,?,?,?,?)",
               (f["roomNo"].strip(), f["building"].strip(), f["roomType"], cap, 1 if f.get("hasProjector") else 0))
            flash("Classroom added.", "ok")
        except (ValueError, AssertionError):
            flash("Enter a valid capacity.", "err")
        except sqlite3.IntegrityError:
            flash("Room number already exists.", "err")
        return redirect(url_for("admin_rooms"))
    return render_template("admin_rooms.html", rooms=q("SELECT * FROM classroom ORDER BY building, roomNo"), ROOM_TYPES=ROOM_TYPES)


@app.route("/admin/rooms/<int:rid>/toggle", methods=["POST"])
@role_required("admin")
def toggle_room(rid):
    room = q("SELECT * FROM classroom WHERE roomId=?", (rid,), one=True)
    if not room:
        abort(404)
    if room["status"] == "Available":
        ex("UPDATE classroom SET status='Maintenance' WHERE roomId=?", (rid,))
        moved, failed = reallocate_room_bookings(rid)
        flash(f"Room {room['roomNo']} under maintenance. {moved} booking(s) moved to other rooms, {failed} cancelled.", "warn")
    else:
        ex("UPDATE classroom SET status='Available' WHERE roomId=?", (rid,))
        flash(f"Room {room['roomNo']} is available again.", "ok")
    return redirect(url_for("admin_rooms"))


@app.route("/admin/rooms/<int:rid>/delete", methods=["POST"])
@role_required("admin")
def delete_room(rid):
    try:
        ex("DELETE FROM classroom WHERE roomId=?", (rid,))
        flash("Classroom deleted.", "ok")
    except sqlite3.IntegrityError:
        flash("Cannot delete: this room has booking history. Mark it as under maintenance instead.", "err")
    return redirect(url_for("admin_rooms"))


@app.route("/admin/courses", methods=["GET", "POST"])
@role_required("admin")
def admin_courses():
    if request.method == "POST":
        f = request.form
        try:
            ex("INSERT INTO course(courseCode,courseName,department,credits) VALUES(?,?,?,?)",
               (f["courseCode"].strip().upper(), f["courseName"].strip(), f["department"].strip(), int(f.get("credits") or 3)))
            flash("Course added.", "ok")
        except (sqlite3.IntegrityError, ValueError):
            flash("Course code already exists or credits invalid.", "err")
        return redirect(url_for("admin_courses"))
    return render_template("admin_courses.html", courses=q("SELECT * FROM course ORDER BY courseCode"))


@app.route("/admin/courses/<int:cid>/delete", methods=["POST"])
@role_required("admin")
def delete_course(cid):
    try:
        ex("DELETE FROM course WHERE courseId=?", (cid,))
        flash("Course deleted.", "ok")
    except sqlite3.IntegrityError:
        flash("Cannot delete: course is used in requests.", "err")
    return redirect(url_for("admin_courses"))


@app.route("/admin/slots", methods=["GET", "POST"])
@role_required("admin")
def admin_slots():
    if request.method == "POST":
        f = request.form
        if f["dayOfWeek"] in DAYS and f["startTime"] < f["endTime"]:
            try:
                ex("INSERT INTO time_slot(dayOfWeek,startTime,endTime) VALUES(?,?,?)", (f["dayOfWeek"], f["startTime"], f["endTime"]))
                flash("Time slot added.", "ok")
            except sqlite3.IntegrityError:
                flash("Slot already exists.", "err")
        else:
            flash("Invalid day or times.", "err")
        return redirect(url_for("admin_slots"))
    return render_template("admin_slots.html", slots=q("SELECT * FROM time_slot ORDER BY startTime, dayOfWeek"))


@app.route("/admin/slots/<int:sid>/delete", methods=["POST"])
@role_required("admin")
def delete_slot(sid):
    try:
        ex("DELETE FROM time_slot WHERE slotId=?", (sid,))
        flash("Time slot deleted.", "ok")
    except sqlite3.IntegrityError:
        flash("Cannot delete: slot is used in requests.", "err")
    return redirect(url_for("admin_slots"))


@app.route("/admin/requests")
@role_required("admin")
def admin_requests():
    status = request.args.get("status", "")
    sql = """SELECT r.*, c.courseCode, f.name fname, s.dayOfWeek, s.startTime, s.endTime, cl.roomNo
             FROM allocation_request r JOIN course c USING(courseId) JOIN faculty f USING(facultyId)
             JOIN time_slot s USING(slotId)
             LEFT JOIN allocation a ON a.requestId=r.requestId AND a.status='Confirmed'
             LEFT JOIN classroom cl ON cl.roomId=a.roomId"""
    args = []
    if status:
        sql += " WHERE r.status=?"
        args.append(status)
    rows = q(sql + " ORDER BY r.requestId DESC LIMIT 200", args)
    return render_template("admin_requests.html", rows=rows, status=status)


@app.route("/admin/users")
@role_required("admin")
def admin_users():
    return render_template("admin_users.html", users=q("""SELECT u.*, f.department FROM user_login u
                           LEFT JOIN faculty f ON f.user_id=u.user_id ORDER BY u.role, u.username"""))


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@role_required("admin")
def delete_user(uid):
    if uid == session["uid"]:
        flash("You cannot delete your own account.", "err")
    else:
        try:
            ex("DELETE FROM user_login WHERE user_id=?", (uid,))
            flash("User deleted.", "ok")
        except sqlite3.IntegrityError:
            flash("Cannot delete: user has booking history.", "err")
    return redirect(url_for("admin_users"))


@app.route("/admin/reports")
@role_required("admin")
def reports():
    frm = parse_date(request.args.get("from"), date.today() - timedelta(days=30)).isoformat()
    to = parse_date(request.args.get("to"), date.today() + timedelta(days=30)).isoformat()
    per_room = q("""SELECT cl.roomNo, cl.building, cl.capacity, COUNT(a.allocationId) n
                    FROM classroom cl LEFT JOIN allocation a ON a.roomId=cl.roomId AND a.status='Confirmed'
                    AND a.reqDate BETWEEN ? AND ? GROUP BY cl.roomId ORDER BY n DESC""", (frm, to))
    by_status = q("SELECT status, COUNT(*) n FROM allocation_request WHERE reqDate BETWEEN ? AND ? GROUP BY status", (frm, to))
    by_faculty = q("""SELECT f.name, f.department, COUNT(*) n FROM allocation_request r JOIN faculty f USING(facultyId)
                      WHERE r.reqDate BETWEEN ? AND ? GROUP BY f.facultyId ORDER BY n DESC LIMIT 8""", (frm, to))
    mx = max([r["n"] for r in per_room] + [1])
    return render_template("reports.html", per_room=per_room, by_status=by_status, by_faculty=by_faculty, mx=mx, frm=frm, to=to)


@app.route("/admin/reports.csv")
@role_required("admin")
def reports_csv():
    rows = q("""SELECT a.reqDate, s.dayOfWeek, s.startTime, s.endTime, cl.roomNo, cl.building,
                c.courseCode, c.courseName, f.name faculty, r.capacityNeeded, a.status
                FROM allocation a JOIN allocation_request r USING(requestId) JOIN time_slot s ON s.slotId=a.slotId
                JOIN classroom cl ON cl.roomId=a.roomId JOIN course c ON c.courseId=r.courseId
                JOIN faculty f ON f.facultyId=r.facultyId ORDER BY a.reqDate, s.startTime""")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Day", "Start", "End", "Room", "Building", "Course Code", "Course", "Faculty", "Strength", "Status"])
    for r in rows:
        w.writerow(list(r))
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=allocations.csv"})


# --------------------------------------------------------------- init + seed
def init_db(seed=True):
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.commit()
    first = db.execute("SELECT COUNT(*) FROM user_login").fetchone()[0] == 0
    db.close()
    if seed and first:
        with app.app_context():
            seed_data()
    ensure_team4()


def ensure_team4():
    """Make sure the admin login TEAM4 / TEAM4@123 exists (also upgrades an old database)."""
    db = sqlite3.connect(DB_PATH)
    row = db.execute("SELECT user_id FROM user_login WHERE username='TEAM4' COLLATE NOCASE").fetchone()
    if not row:
        ph = generate_password_hash("TEAM4@123")
        old = db.execute("SELECT user_id FROM user_login WHERE role='admin' ORDER BY user_id LIMIT 1").fetchone()
        if old:
            db.execute("UPDATE user_login SET username='TEAM4', password_hash=?, display_name='TEAM 4 Administrator' WHERE user_id=?", (ph, old[0]))
        else:
            db.execute("INSERT INTO user_login(username,password_hash,role,display_name) VALUES('TEAM4',?,'admin','TEAM 4 Administrator')", (ph,))
        db.commit()
    db.close()


def seed_data():
    for day in DAYS:
        for s, e in PERIODS:
            ex("INSERT OR IGNORE INTO time_slot(dayOfWeek,startTime,endTime) VALUES(?,?,?)", (day, s, e))
    rooms = [("A101", "Block A", "Classroom", 60, 1), ("A102", "Block A", "Classroom", 60, 1),
             ("A201", "Block A", "Classroom", 40, 1), ("A202", "Block A", "Classroom", 40, 0),
             ("B101", "Block B", "Seminar Hall", 150, 1), ("B102", "Block B", "Classroom", 80, 1),
             ("C101", "Block C", "Classroom", 30, 0), ("CSLAB1", "CSE Block", "Laboratory", 30, 1),
             ("CSLAB2", "CSE Block", "Laboratory", 30, 1), ("PHYLAB", "Science Block", "Laboratory", 25, 0)]
    for r in rooms:
        ex("INSERT INTO classroom(roomNo,building,roomType,capacity,hasProjector) VALUES(?,?,?,?,?)", r)
    courses = [("CS301", "Software Engineering", "CSE", 4), ("CS302", "Database Management Systems", "CSE", 4),
               ("CS303", "Operating Systems", "CSE", 3), ("CS304", "Computer Networks", "CSE", 3),
               ("CS305", "DBMS Laboratory", "CSE", 2), ("MA201", "Discrete Mathematics", "Maths", 3),
               ("PH101", "Engineering Physics", "Physics", 3)]
    for c in courses:
        ex("INSERT INTO course(courseCode,courseName,department,credits) VALUES(?,?,?,?)", c)
    ex("INSERT INTO user_login(username,password_hash,role,display_name) VALUES(?,?,?,?)",
       ("TEAM4", generate_password_hash("TEAM4@123"), "admin", "TEAM 4 Administrator"))
    for uname, name, dept in [("ravi", "Dr. Ravi Kumar", "CSE"), ("sita", "Prof. Sita Devi", "CSE"), ("anil", "Dr. Anil Reddy", "Maths")]:
        uid = ex("INSERT INTO user_login(username,password_hash,role,display_name) VALUES(?,?,?,?)",
                 (uname, generate_password_hash("faculty123"), "faculty", name))
        ex("INSERT INTO faculty(user_id,name,email,department,phone) VALUES(?,?,?,?,?)",
           (uid, name, f"{uname}@college.edu", dept, "9000000000"))
    ex("INSERT INTO user_login(username,password_hash,role,display_name,batch) VALUES(?,?,?,?,?)",
       ("student1", generate_password_hash("student123"), "student", "Student One", "CSE-3A"))
    # a few demo bookings in the coming week
    monday = week_start(date.today())
    if monday < date.today():
        monday += timedelta(days=7)
    demo = [(1, 1, 0, 0, 55, 1), (1, 2, 0, 1, 58, 1), (2, 3, 1, 0, 40, 1), (2, 5, 2, 4, 28, 0), (3, 6, 3, 2, 70, 1)]
    for fid, cid, dayi, per, need, proj in demo:
        d = monday + timedelta(days=dayi)
        slot = q("SELECT slotId FROM time_slot WHERE dayOfWeek=? AND startTime=?", (DAYS[dayi], PERIODS[per][0]), one=True)
        rid = ex("""INSERT INTO allocation_request(facultyId,courseId,slotId,reqDate,capacityNeeded,needProjector,roomType)
                    VALUES(?,?,?,?,?,?,?)""", (fid, cid, slot["slotId"], d.isoformat(), need, proj, "Laboratory" if cid == 5 else "Any"))
        process_request(rid)


init_db()

if __name__ == "__main__":
    app.run(debug=True)
