import requests
from utils.config import PUBLIC_API_KEY


class PublicAPI:
    def __init__(self):
        self.base_url = "https://api.public.com"  # This is an assumption
        self.api_key = PUBLIC_API_KEY
        self.headers = {
            "Authorization": f"Bearer {self.api_key}"
        }

    def _make_request(self, method, endpoint, **kwargs):
        url = f"{self.base_url}/{endpoint}"
        try:
            response = requests.request(
                method, url, headers=self.headers, **kwargs
            )
            # Raise an exception for bad status codes
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"An error occurred: {e}")
            return None

    def get_accounts(self):
        """Get a list of accounts."""
        return self._make_request("GET", "accounts")

    def get_account_portfolio(self, account_id):
        """Get the portfolio for a specific account."""
        endpoint = f"accounts/{account_id}/portfolio-v2"
        return self._make_request("GET", endpoint)

    def get_order_history(self, account_id):
        """Get the order history for a specific account."""
        endpoint = f"accounts/{account_id}/history"
        return self._make_request("GET", endpoint)

    def place_order(self, order_data):
        """Place a new order."""
        return self._make_request("POST", "orders", json=order_data)

    def cancel_order(self, order_id):
        """Cancel an existing order."""
        endpoint = f"orders/{order_id}"
        return self._make_request("DELETE", endpoint)
