from io import StringIO

import requests
import pandas as pd
from bs4 import BeautifulSoup


class ScreenerScraper:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        # A session object retains cookies and authentications across requests
        self.session = requests.Session()
        # Using a User-Agent helps bypass "403 Forbidden" errors
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }

    def login(self):
        login_url = "https://www.screener.in/login/"

        # 1. Preliminary GET request to retrieve the CSRF token from the HTML
        response = self.session.get(login_url, headers=self.headers)

        # 2. Parse the HTML content to convert the raw HTML into a parse tree
        soup = BeautifulSoup(response.text, "html.parser")

        # Extract the hidden CSRF token needed for the POST request
        csrf_token = soup.find("input", {"name": "csrfmiddlewaretoken"})["value"]

        login_data = {
            "csrfmiddlewaretoken": csrf_token,
            "username": self.username,
            "password": self.password,
        }

        # 3. Send a POST request with credentials
        # Include Referer so Django CSRF validation accepts the POST
        post_headers = {**self.headers, "Referer": login_url}
        self.session.post(login_url, data=login_data, headers=post_headers)
        print("Session authenticated.")

    def scrape_screen(self, screen_url, output_filename="data/screened_stocks.csv"):
        # Fetch HTML content of the target webpage
        response = self.session.get(screen_url, headers=self.headers)
        soup = BeautifulSoup(response.text, "html.parser")

        # Find the data table
        table = soup.find("table")

        if table:
            # Extract NSE symbols from company links (e.g. /company/SRF/)
            symbols = []
            for tr in table.select("tr[data-row-company-id]"):
                link = tr.select_one('a[href*="/company/"]')
                if link:
                    # href like /company/SRF/consolidated/ or /company/PGHH/
                    symbols.append(link["href"].strip("/").split("/")[1])
                else:
                    symbols.append(None)

            # Extract data into a Pandas DataFrame
            # StringIO avoids pandas treating the HTML string as a filename
            df = pd.read_html(StringIO(str(table)))[0]
            # Clean empty rows and Screener's repeated mid-table header row
            df = df.dropna(subset=["Name"])
            df = df[df["Name"].astype(str).str.strip().str.lower() != "name"]
            if len(symbols) == len(df):
                df.insert(1, "Symbol", symbols)
            else:
                print(
                    f"Warning: symbol count ({len(symbols)}) != row count ({len(df)}); "
                    "saving without Symbol column."
                )
            df.to_csv(output_filename, index=False)
            print(f"Data successfully saved to {output_filename}")
            return df
        else:
            print("Could not locate the table. Check the URL or login status.")
            return None


# Execution Block
if __name__ == "__main__":
    scraper = ScreenerScraper("YOUR_EMAIL", "YOUR_PASSWORD")
    scraper.login()

    # Define your screens as a dictionary
    screens = {
        "piotroski_stocks.csv": "https://www.screener.in/screens/1698348/piotroski-scan/",
        "capacity_expansion.csv": "https://www.screener.in/screens/897734/capacity-expansion-stocks/",
        "coffee_can.csv": "https://www.screener.in/screens/175680/coffee-can-portfolio-saurabh-mukherjea/",
        "growth_no_dilution.csv": "https://www.screener.in/screens/226712/growth-without-dilution/",
        "rsi_oversold.csv": "https://www.screener.in/screens/985942/rsi-oversold-stocks/",
    }

    for filename, url in screens.items():
        print(f"Scraping {filename}...")
        scraper.scrape_screen(url, output_filename=f"data/{filename}")
