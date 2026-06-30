# TrafficSim HCM

A real time traffic simulation and reinforcement learning based signal control system for a simulated map of Ho Chi Minh City. The backend (FastAPI) runs a continuous simulation of vehicles, pedestrians, and traffic lights, controlled by a Q learning reinforcement learning agent and a supervised congestion classifier. The frontend is a live, interactive map dashboard built with MapLibre GL.

This guide covers every step needed to run the project from the moment the zip file is downloaded.

## 1. Requirements

* Python 3.11 or later (the project was built and tested on Python 3.11.7)
* pip
* A modern web browser (Chrome, Firefox, Edge, or similar)
* An internet connection (the frontend loads MapLibre GL and map tiles from a CDN, and the dashboard needs this to display the map)

No database setup is required. The project uses SQLite, and the database file is created automatically the first time the server runs.

## 2. Unzip the project

Download `finalai-main.zip` and extract it. You should end up with a folder structure similar to the following.

```
finalai-main/
  backend/
    main.py
    reinforcement.py
    supervised.py
    requirements.txt
    OOP/
    database/
  frontend/
    index.html
    map.js
  README.md
```

Open a terminal and navigate into the extracted folder.

```
cd finalai-main
```

## 3. Create and activate a virtual environment

It is recommended to use a virtual environment so the project's dependencies do not conflict with other Python projects on your machine.

On macOS or Linux:

```
python3 -m venv backend/venv
source backend/venv/bin/activate
```

On Windows (Command Prompt):

```
python -m venv backend\venv
backend\venv\Scripts\activate
```

If a `venv` folder is already included in the downloaded zip, it is safe to delete it and create a fresh one with the commands above, since virtual environments are tied to the exact machine and Python installation they were created on and will usually not work correctly if copied to a different computer.

## 4. Install dependencies

With the virtual environment activated, install the required Python packages from the backend folder.

```
pip install -r backend/requirements.txt
```

This installs FastAPI, Uvicorn (the web server), and the WebSocket library used for real time communication between the backend and the dashboard.

## 5. Run the backend server

From the project root folder (the folder containing `backend` and `frontend`), run the following.

```
cd backend
python main.py
```

You should see log output similar to the following, confirming that the simulation has started and seeded its initial vehicles, traffic lights, and signs.

```
INFO:     Started server process
INFO:     Waiting for application startup.
[TrafficSim] Started — 120 xe, 40 đèn, 60 biển
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

Keep this terminal window open. The server needs to keep running for the simulation and dashboard to work.

## 6. Open the dashboard

The backend automatically serves the frontend, so no separate frontend server is needed. Open a web browser and go to the following address.

```
http://localhost:8000
```

You should see a live map of Ho Chi Minh City with moving vehicles, traffic lights cycling through red, yellow, and green, pedestrians, and a real time machine learning decision log showing the reinforcement learning agent's actions.

## 7. Stopping the server

To stop the simulation, return to the terminal window running `python main.py` and press `Ctrl+C`. The next time the server starts, it will resume using the same SQLite database file located at `backend/database/traffic_sim.db`, so prior reinforcement learning samples and history are preserved.

## 8. Optional: resetting the simulation data

The simulation logs a large amount of data to `backend/database/traffic_sim.db` over time. To start completely fresh, stop the server, delete this file (along with any `traffic_sim.db-shm` and `traffic_sim.db-wal` files in the same folder), and start the server again. A new, empty database will be created automatically.

## 9. Troubleshooting

If the dashboard loads but the map tiles do not appear, this is most likely because the embedded MapTiler API key in `frontend/map.js` has reached its usage limit or been revoked, since it is shared across anyone running this project; the simulation itself (vehicles, lights, and the machine learning log) will still function even if the map tiles fail to load. If port 8000 is already in use on your machine, either stop the other process using that port, or edit the last line of `backend/main.py` to use a different port and update `wsUrl` and `apiBase` in `frontend/map.js` to match.
