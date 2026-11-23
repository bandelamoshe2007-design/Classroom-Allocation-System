# Classroom Allocation System (Flask + SQLite)

## Run in VS Code
1. Open this folder in VS Code (File > Open Folder).
2. Open Terminal (Ctrl + `) and run:
   - Windows:  `python -m venv venv`  then  `venv\Scripts\activate`
   - Mac/Linux: `python3 -m venv venv`  then  `source venv/bin/activate`
3. `pip install -r requirements.txt`
4. `python app.py`
5. Open http://127.0.0.1:5000 in the browser. The database is created automatically with demo data.

## Demo logins
- Admin (team login): TEAM4 / TEAM4@123
- Faculty: ravi / faculty123  (also sita, anil)
- Student: student1 / student123

## Features
Role-based login, classroom/slot/course management, room requests with automatic best-fit allocation,
clash detection with alternative suggestions, availability grid, weekly timetable, notifications,
reports with CSV download, auto re-allocation when a room goes under maintenance.

To reset all data, stop the app and delete `classroom.db`.
