from alpha_vantage.timeseries import TimeSeries
from utils.config import ALPHA_VANTAGE_API_KEY


class AlphaVantageAPI:
    def __init__(self):
        self.api_key = ALPHA_VANTAGE_API_KEY
        self.ts = TimeSeries(key=self.api_key, output_format='pandas')

    def get_daily_prices(self, symbol, output_size='full'):
        """
        Get daily time series data for a given symbol.
        """
        try:
            data, meta_data = self.ts.get_daily(
                symbol=symbol, outputsize=output_size
            )
            return data
        except Exception as e:
            print(f"Could not fetch data from Alpha Vantage: {e}")
            return None
