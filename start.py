"""Entry point — initializes the DB and runs the app."""
from app import app, init_db

if __name__ == "__main__":
    init_db()
    app.run(debug=False, host="0.0.0.0", port=5000)
