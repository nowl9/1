import requests
from utils.config import DEEPSEEK_API_KEY


class DeepseekAPI:
    def __init__(self):
        self.base_url = "https://api.deepseek.com/v1"  # This is an assumption
        self.api_key = DEEPSEEK_API_KEY
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

    def generate_text(self, prompt, model="deepseek-chat", max_tokens=1024,
                      temperature=0.7):
        """
        Generate text using the Deepseek LLM.
        """
        endpoint = "chat/completions"
        data = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        url = f"{self.base_url}/{endpoint}"
        try:
            response = requests.post(url, headers=self.headers, json=data)
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except requests.exceptions.RequestException as e:
            print(f"An error occurred: {e}")
            return None
        except (KeyError, IndexError) as e:
            print(f"Error parsing response from Deepseek API: {e}")
            print(f"Response: {response.text}")
            return None
