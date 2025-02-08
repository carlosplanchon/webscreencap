#!/usr/bin/env python3

# python -m pip install -U requests

import requests
import base64

API_URL = "http://127.0.0.1:37563/screenshot"
API_KEY = "1234"  # Replace with your actual API key

# Test the API with an example URL
url: str = "https://tiendainglesa.com.uy/"  # Replace with the URL you want to test

headers = {
    "API-Key": API_KEY
}
params = {
    "url": url
}

response = requests.get(API_URL, headers=headers, params=params)

if response.status_code == 200:
    data = response.json()

    # Decode and save the screenshot
    screenshot_data = base64.b64decode(data["screenshot"])
    with open("screenshot.png", "wb") as screenshot_file:
        screenshot_file.write(screenshot_data)

    # Decode and save the HTML content
    html_data = base64.b64decode(data["html"]).decode("utf-8")
    with open("page.html", "w", encoding="utf-8") as html_file:
        html_file.write(html_data)

    print("Screenshot and HTML content saved successfully.")
else:
    print(f"Error: {response.status_code} - {response.text}")
