from api.alpha_vantage_api import AlphaVantageAPI
from ml.predictive_model import PredictiveModel
from trading.trader import Trader
import os
import json


class Backtester:
    def __init__(self, app):
        self.app = app
        self.alpha_vantage_api = AlphaVantageAPI()

    def run_backtest(self, symbol, start_date, end_date, initial_cash):
        print("--- Running Backtest ---")

        # 1. Fetch historical data
        historical_data = self.alpha_vantage_api.get_daily_prices(symbol)
        if historical_data is None or historical_data.empty:
            print("Could not fetch historical data for backtest.")
            self.display_results({"trades": ["Could not fetch data."]})
            return

        # Filter data for the backtest period
        backtest_data = historical_data[start_date:end_date]

        # 2. Initialize portfolio
        portfolio = {
            "cash": initial_cash,
            "positions": {},
            "pnl": 0,
            "trades": []
        }

        # 3. Initialize model
        model = PredictiveModel(symbol)
        if not os.path.exists(model.model_path):
            print(f"Training model for {symbol} for backtest...")
            model.train(historical_data)

        # 4. Backtesting loop
        for date, row in backtest_data.iterrows():
            # Get ML prediction for the next day
            # In a real backtest, you'd predict on data available up to 'date'
            prediction_data = historical_data[:date]
            prediction = model.predict(prediction_data)

            # Get LLM suggestion
            temp_trader = Trader(self.app, None, self.app.trader.deepseek_api)
            prompt = temp_trader.construct_prompt(
                portfolio, self.app.risk_tolerance.get(), symbol, prediction
            )

            suggestions = self.app.trader.deepseek_api.generate_text(prompt)

            try:
                suggestions_data = json.loads(suggestions)
                if "suggestions" in suggestions_data:
                    for trade in suggestions_data["suggestions"]:
                        self.simulate_trade(trade, row, portfolio)
            except (json.JSONDecodeError, TypeError):
                pass  # No valid suggestions

        # 5. Display results
        self.display_results(portfolio)
        print("--- Backtest Finished ---")

    def simulate_trade(self, trade, data_row, portfolio):
        # This is a simplified simulation
        price = data_row['4. close']
        quantity = trade.get('quantity', 1)
        cost = price * quantity * 100  # Options contract size

        if trade.get('strategy') in ['buy_call', 'buy_put']:
            if portfolio['cash'] >= cost:
                portfolio['cash'] -= cost
                portfolio['trades'].append(
                    f"BOUGHT {quantity} {trade.get('ticker')} "
                    f"{trade.get('strategy')} at {price} on "
                    f"{data_row.name.date()}"
                )

    def display_results(self, portfolio):
        results = "--- Backtest Results ---\n"
        results += f"Final Cash: ${portfolio.get('cash', 0):.2f}\n"
        results += f"Total Trades: {len(portfolio.get('trades', []))}\n"
        results += "\n--- Trades ---\n"
        results += "\n".join(portfolio.get('trades', []))

        # Update GUI
        self.app.backtest_results_text.config(state="normal")
        self.app.backtest_results_text.delete("1.0", "end")
        self.app.backtest_results_text.insert("1.0", results)
        self.app.backtest_results_text.config(state="disabled")
