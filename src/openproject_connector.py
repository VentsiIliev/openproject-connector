import requests
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter

from config import API_KEY, OPENPROJECT_URL


class OpenProjectConnector:
    def __init__(self, base_url: str = OPENPROJECT_URL, api_key: str = API_KEY):
        if not base_url:
            raise ValueError("OPENPROJECT_URL is not configured.")
        if not api_key:
            raise ValueError("OPENPROJECT_API_KEY is not configured.")

        self.base_url = base_url.rstrip("/")
        self.api_base = f"{self.base_url}/api/v3"

        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth("apikey", api_key)
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip, deflate",
            }
        )

        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def get(self, endpoint: str, **kwargs):
        url = self._build_url(endpoint)
        response = self.session.get(url, timeout=30, **kwargs)
        response.raise_for_status()
        return response.json()

    def patch(self, endpoint: str, payload: dict, **kwargs):
        url = self._build_url(endpoint)
        response = self.session.patch(url, json=payload, timeout=30, **kwargs)
        response.raise_for_status()
        return response.json()

    def test_connection(self):
        return self.get("/")

    def _build_url(self, endpoint: str) -> str:
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return endpoint

        if endpoint.startswith("/api/v3"):
            return f"{self.base_url}{endpoint}"

        return f"{self.api_base}/{endpoint.lstrip('/')}"
