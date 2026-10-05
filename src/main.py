from app import app
from config import APP_HOST, APP_PORT


if __name__ == "__main__":
    app.run(debug=True, host=APP_HOST, port=APP_PORT)
