import tkinter as tk
from tkinter import ttk
from api.public_api import PublicAPI
from api.deepseek_api import DeepseekAPI
from trading.trader import Trader
from trading.backtester import Backtester
import threading


class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("LLM Trader")
        self.geometry("800x600")

        self.public_api = PublicAPI()
        self.deepseek_api = DeepseekAPI()
        self.trader = Trader(self, self.public_api, self.deepseek_api)
        self.backtester = Backtester(self)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(pady=10, expand=True)

        # Dashboard Tab
        dashboard_frame = ttk.Frame(self.notebook, width=800, height=600)
        dashboard_frame.pack(fill="both", expand=True)
        self.notebook.add(dashboard_frame, text="Dashboard")
        self.create_dashboard_widgets(dashboard_frame)

        # Backtesting Tab
        backtesting_frame = ttk.Frame(self.notebook, width=800, height=600)
        backtesting_frame.pack(fill="both", expand=True)
        self.notebook.add(backtesting_frame, text="Backtesting")
        self.create_backtesting_widgets(backtesting_frame)

        # Settings Tab
        settings_frame = ttk.Frame(self.notebook, width=800, height=600)
        settings_frame.pack(fill="both", expand=True)
        self.notebook.add(settings_frame, text="Settings")
        self.create_settings_widgets(settings_frame)

        # Daily Summary Tab
        summary_frame = ttk.Frame(self.notebook, width=800, height=600)
        summary_frame.pack(fill="both", expand=True)
        self.notebook.add(summary_frame, text="Daily Summary")
        self.create_summary_widgets(summary_frame)

        self.update_dashboard()

    def create_dashboard_widgets(self, parent_frame):
        # P&L section
        pnl_frame = ttk.LabelFrame(parent_frame, text="Performance")
        pnl_frame.grid(row=0, column=0, padx=10, pady=10, sticky="ew")

        ttk.Label(pnl_frame, text="Total Account Value:").grid(
            row=0, column=0, padx=5, pady=5)
        self.account_value_label = ttk.Label(pnl_frame, text="$0.00")
        self.account_value_label.grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(pnl_frame, text="Daily P&L:").grid(
            row=1, column=0, padx=5, pady=5)
        self.daily_pnl_label = ttk.Label(pnl_frame, text="$0.00")
        self.daily_pnl_label.grid(row=1, column=1, padx=5, pady=5)

        ttk.Label(pnl_frame, text="Weekly P&L:").grid(
            row=2, column=0, padx=5, pady=5)
        self.weekly_pnl_label = ttk.Label(pnl_frame, text="$0.00")
        self.weekly_pnl_label.grid(row=2, column=1, padx=5, pady=5)

        ttk.Label(pnl_frame, text="Monthly P&L:").grid(
            row=3, column=0, padx=5, pady=5)
        self.monthly_pnl_label = ttk.Label(pnl_frame, text="$0.00")
        self.monthly_pnl_label.grid(row=3, column=1, padx=5, pady=5)

        # Trading switch
        trading_frame = ttk.LabelFrame(parent_frame, text="Automated Trading")
        trading_frame.grid(row=0, column=1, padx=10, pady=10, sticky="ew")

        self.trading_status = tk.StringVar(value="OFF")
        self.trading_status.trace("w", self.toggle_trading)
        on_button = ttk.Radiobutton(
            trading_frame, text="ON", variable=self.trading_status, value="ON")
        off_button = ttk.Radiobutton(
            trading_frame, text="OFF", variable=self.trading_status,
            value="OFF")
        on_button.pack(side="left", padx=5, pady=5)
        off_button.pack(side="left", padx=5, pady=5)

        # Orders section
        orders_frame = ttk.LabelFrame(parent_frame, text="Recent Orders")
        orders_frame.grid(row=1, column=0, columnspan=2, padx=10, pady=10,
                          sticky="nsew")

        self.orders_list = tk.Listbox(orders_frame, height=10)
        self.orders_list.pack(fill="both", expand=True, padx=5, pady=5)

        refresh_button = ttk.Button(
            orders_frame, text="Refresh", command=self.update_dashboard)
        refresh_button.pack(pady=5)

    def create_backtesting_widgets(self, parent_frame):
        controls_frame = ttk.LabelFrame(parent_frame, text="Backtest Controls")
        controls_frame.pack(padx=10, pady=10, fill="x")

        ttk.Label(controls_frame, text="Symbol:").grid(
            row=0, column=0, padx=5, pady=5)
        self.backtest_symbol = tk.StringVar(value="SPY")
        ttk.Entry(controls_frame, textvariable=self.backtest_symbol).grid(
            row=0, column=1, padx=5, pady=5)

        ttk.Label(controls_frame, text="Start Date (YYYY-MM-DD):").grid(
            row=1, column=0, padx=5, pady=5)
        self.backtest_start_date = tk.StringVar(value="2023-01-01")
        ttk.Entry(controls_frame, textvariable=self.backtest_start_date).grid(
            row=1, column=1, padx=5, pady=5)

        ttk.Label(controls_frame, text="End Date (YYYY-MM-DD):").grid(
            row=2, column=0, padx=5, pady=5)
        self.backtest_end_date = tk.StringVar(value="2023-12-31")
        ttk.Entry(controls_frame, textvariable=self.backtest_end_date).grid(
            row=2, column=1, padx=5, pady=5)

        ttk.Label(controls_frame, text="Initial Cash:").grid(
            row=3, column=0, padx=5, pady=5)
        self.backtest_initial_cash = tk.DoubleVar(value=100000.0)
        ttk.Entry(
            controls_frame, textvariable=self.backtest_initial_cash
        ).grid(row=3, column=1, padx=5, pady=5)

        run_button = ttk.Button(
            controls_frame, text="Run Backtest",
            command=self.run_backtest_thread)
        run_button.grid(row=4, column=0, columnspan=2, pady=10)

        results_frame = ttk.LabelFrame(parent_frame, text="Backtest Results")
        results_frame.pack(padx=10, pady=10, fill="both", expand=True)

        self.backtest_results_text = tk.Text(results_frame, height=15)
        self.backtest_results_text.pack(
            fill="both", expand=True, padx=5, pady=5)
        self.backtest_results_text.insert(
            tk.END, "Backtest results will be shown here.")
        self.backtest_results_text.config(state="disabled")

    def create_settings_widgets(self, parent_frame):
        settings_frame = ttk.LabelFrame(parent_frame, text="LLM Settings")
        settings_frame.pack(padx=10, pady=10, fill="both", expand=True)

        ttk.Label(settings_frame, text="LLM Model:").grid(
            row=0, column=0, padx=5, pady=5, sticky="w")
        self.llm_model = tk.StringVar(value="deepseek-v3")
        ttk.Entry(settings_frame, textvariable=self.llm_model).grid(
            row=0, column=1, padx=5, pady=5, sticky="ew")

        ttk.Label(settings_frame, text="Temperature:").grid(
            row=1, column=0, padx=5, pady=5, sticky="w")
        self.temperature = tk.DoubleVar(value=0.7)
        ttk.Scale(
            settings_frame, from_=0, to=1, variable=self.temperature,
            orient="horizontal"
        ).grid(row=1, column=1, padx=5, pady=5, sticky="ew")

        ttk.Label(settings_frame, text="Risk Tolerance:").grid(
            row=2, column=0, padx=5, pady=5, sticky="w")
        self.risk_tolerance = tk.StringVar(value="Medium")
        ttk.Combobox(
            settings_frame, textvariable=self.risk_tolerance,
            values=["Low", "Medium", "High"]
        ).grid(row=2, column=1, padx=5, pady=5, sticky="ew")

    def create_summary_widgets(self, parent_frame):
        summary_frame = ttk.LabelFrame(parent_frame, text="Daily Summary")
        summary_frame.pack(padx=10, pady=10, fill="both", expand=True)

        self.summary_text = tk.Text(summary_frame, height=15)
        self.summary_text.pack(fill="both", expand=True, padx=5, pady=5)
        self.summary_text.insert(
            tk.END, "Click 'Generate Summary' to get the daily report.")
        self.summary_text.config(state="disabled")

        generate_button = ttk.Button(
            summary_frame, text="Generate Summary",
            command=self.trader.generate_daily_summary)
        generate_button.pack(pady=5)

    def update_dashboard(self):
        """Fetch data from the Public.com API and update the dashboard."""
        # This is where I'll need to make some assumptions about the API
        # response. I'll assume get_accounts() returns a list of accounts
        # and I'll use the first one.
        accounts = self.public_api.get_accounts()
        if accounts and len(accounts) > 0:
            # Assuming the first account is the one we want to use
            account_id = accounts[0].get("id")
            if account_id:
                # Update account value
                portfolio = self.public_api.get_account_portfolio(account_id)
                if portfolio and "total_value" in portfolio:
                    total_value = float(portfolio["total_value"])
                    self.account_value_label.config(
                        text=f"${total_value:,.2f}")

                # Update order history
                orders = self.public_api.get_order_history(account_id)
                self.orders_list.delete(0, tk.END)  # Clear the listbox
                if orders:
                    for order in orders:
                        # Assuming order is a dictionary with keys like
                        # 'symbol', 'side', 'quantity', 'status'
                        order_str = (
                            f"{order.get('symbol', 'N/A')} - "
                            f"{order.get('side', 'N/A')} "
                            f"{order.get('quantity', 'N/A')} @ "
                            f"{order.get('average_price', 'N/A')} - "
                            f"{order.get('status', 'N/A')}"
                        )
                        self.orders_list.insert(tk.END, order_str)
        else:
            # Handle case where no accounts are found
            self.account_value_label.config(text="N/A")
            self.orders_list.delete(0, tk.END)
            self.orders_list.insert(
                tk.END, "Could not fetch account information.")

    def run_backtest_thread(self):
        # Run the backtest in a separate thread to avoid freezing the GUI
        thread = threading.Thread(target=self.run_backtest)
        thread.daemon = True
        thread.start()

    def run_backtest(self):
        symbol = self.backtest_symbol.get()
        start_date = self.backtest_start_date.get()
        end_date = self.backtest_end_date.get()
        initial_cash = self.backtest_initial_cash.get()

        self.backtester.run_backtest(
            symbol, start_date, end_date, initial_cash)

    def toggle_trading(self, *args):
        if self.trading_status.get() == "ON":
            self.trader.start_trading()
        else:
            self.trader.stop_trading()

    def on_closing(self):
        print("Closing application...")
        self.trader.stop_trading()
        self.destroy()


if __name__ == '__main__':
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
