import threading
import time
import json
from api.public_api import PublicAPI
from api.deepseek_api import DeepseekAPI


class Trader:
    def __init__(self, app, public_api: PublicAPI, deepseek_api: DeepseekAPI):
        self.app = app
        self.public_api = public_api
        self.deepseek_api = deepseek_api
        self.trading = False
        self.thread = None
        self.daily_trades = []

    def start_trading(self):
        if not self.trading:
            print("---DISCLAIMER---")
            print("Executing live trades based on LLM suggestions is "
                  "extremely risky.")
            print("You are responsible for any financial losses.")
            print("---DISCLAIMER---")
            self.trading = True
            self.thread = threading.Thread(target=self.trading_loop)
            # Allows main program to exit even if thread is running
            self.thread.daemon = True
            self.thread.start()
            print("Trading started.")

    def stop_trading(self):
        if self.trading:
            self.trading = False
            if self.thread and self.thread.is_alive():
                self.thread.join()  # Wait for the thread to finish
            print("Trading stopped.")

    def trading_loop(self):
        while self.trading:
            print("Running trading loop...")
            try:
                # 1. Fetch data
                accounts = self.public_api.get_accounts()
                if not accounts:
                    print("Could not fetch accounts. "
                          "Skipping this iteration.")
                    time.sleep(60)
                    continue

                account_id = accounts[0].get("id")
                portfolio = self.public_api.get_account_portfolio(account_id)

                # 2. Construct prompt
                risk_tolerance = self.app.risk_tolerance.get()
                prompt = self.construct_prompt(portfolio, risk_tolerance)

                # 3. Get suggestions from LLM
                model = self.app.llm_model.get()
                temperature = self.app.temperature.get()
                suggestions = self.deepseek_api.generate_text(
                    prompt, model=model, temperature=temperature
                )

                # 4. Parse and execute suggestions
                try:
                    suggestions_data = json.loads(suggestions)
                    if "suggestions" in suggestions_data:
                        for trade in suggestions_data["suggestions"]:
                            self.execute_trade(account_id, trade)
                except json.JSONDecodeError:
                    print(f"Could not decode LLM response as JSON: "
                          f"{suggestions}")

                # Wait for the next iteration
                time.sleep(60)  # 1 minute interval

            except Exception as e:
                print(f"An error occurred in the trading loop: {e}")
                time.sleep(60)

    def construct_prompt(self, portfolio, risk_tolerance):
        prompt = f"""
        You are an expert options trading LLM. Your goal is to maximize profit
        while managing risk.
        Your stated risk tolerance is: {risk_tolerance}. Please tailor your
        suggestions accordingly.

        Here is the current portfolio status:
        - Total Value: ${portfolio.get('total_value', 'N/A')}
        - Equity: ${portfolio.get('equity', 'N/A')}
        - Cash Balance: ${portfolio.get('cash_balance', 'N/A')}

        Current Positions:
        """
        positions = portfolio.get('positions', [])
        if positions:
            for p in positions:
                prompt += (f"- {p.get('symbol')}: {p.get('quantity')} shares "
                           f"@ ${p.get('average_price')}\n")
        else:
            prompt += "- No open positions.\n"

        prompt += """
        Based on the current market conditions and the portfolio, what is your
        analysis and what specific, actionable options trading suggestions do
        you have for the next hour?

        Please provide your response as a JSON object with a single key
        "suggestions" which is a list of trade objects.
        Each trade object should have the following keys: "ticker",
        "strategy" (e.g., "buy_call", "sell_put"), "strike_price",
        "expiration_date" (in YYYY-MM-DD format), "quantity", and "reasoning".
        If you have no suggestions, return an empty list.
        Example:
        {
          "suggestions": [
            {
              "ticker": "AAPL",
              "strategy": "buy_call",
              "strike_price": 150.0,
              "expiration_date": "2025-12-31",
              "quantity": 1,
              "reasoning": "Based on recent positive news and bullish market
                           sentiment, a call option on AAPL is likely to be
                           profitable."
            }
          ]
        }
        """
        return prompt

    def execute_trade(self, account_id, trade):
        print(f"Executing trade: {trade}")
        # This is where you would map the LLM suggestion to the Public.com API
        # order format. This is a complex step and requires careful handling
        # of different strategies. For now, we will just log the trade.

        # Example of what the order data might look like for a single leg
        # option
        # order_data = {
        #     "account_id": account_id,
        #     "symbol": trade["ticker"],
        #     "side": "buy",  # or "sell"
        #     "quantity": trade["quantity"],
        #     "order_type": "market",  # or "limit"
        #     "time_in_force": "day",
        #     "legs": [
        #         {
        #             "side": "buy",  # or "sell"
        #             "option_symbol":
        #                 f"{trade['ticker']}"
        #                 f"{trade['expiration_date'].replace('-', '')}"
        #                 f"C{str(int(trade['strike_price']*1000)).zfill(8)}",
        #             "quantity": trade["quantity"]
        #         }
        #     ]
        # }
        # result = self.public_api.place_order(order_data)
        # print(f"Order result: {result}")

        self.daily_trades.append(trade)
        self.recursive_learning_step(trade, "simulated_success")  # Placeholder

    def recursive_learning_step(self, trade, outcome):
        print("--- Recursive Learning Step ---")
        print(f"Trade: {trade}")
        print(f"Outcome: {outcome}")
        print("In a real implementation, this is where the model would be "
              "updated or the prompt would be refined based on the trade's "
              "outcome.")
        print("-----------------------------")

    def generate_daily_summary(self):
        print("Generating daily summary...")

        # In a real app, you'd have a record of the day's trades.
        # For now, we'll just use a placeholder.
        if not self.daily_trades:
            self.daily_trades.append("No trades were executed today.")

        prompt = f"""
        You are an expert trading analyst. Please provide a summary of the
        following trading activity.

        Trades:
        {self.daily_trades}

        Based on this activity, what was the overall strategy and performance?
        What could be improved for tomorrow?
        """

        summary = self.deepseek_api.generate_text(prompt)

        # Update the GUI with the summary
        self.app.summary_text.config(state="normal")
        self.app.summary_text.delete("1.0", "end")
        self.app.summary_text.insert("1.0", summary)
        self.app.summary_text.config(state="disabled")

        print("Daily summary generated.")
