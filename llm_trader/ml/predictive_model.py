import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error
import joblib
import os


class PredictiveModel:
    def __init__(self, symbol):
        self.symbol = symbol
        self.model = xgb.XGBRegressor(
            objective='reg:squarederror', n_estimators=1000
        )
        self.model_path = f"llm_trader/ml/models/{self.symbol}_model.joblib"

    def _prepare_data(self, df):
        """
        Feature engineering: Create lagged features and moving averages.
        """
        df['returns'] = df['4. close'].pct_change()
        df['MA_7'] = df['4. close'].rolling(window=7).mean()
        df['MA_21'] = df['4. close'].rolling(window=21).mean()

        for i in range(1, 8):
            df[f'lag_{i}'] = df['returns'].shift(i)

        df = df.dropna()

        X = df[['MA_7', 'MA_21'] + [f'lag_{i}' for i in range(1, 8)]]
        y = df['returns']

        return X, y

    def train(self, data):
        """
        Train the XGBoost model.
        """
        X, y = self._prepare_data(data)

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, shuffle=False
        )

        self.model.fit(X_train, y_train,
                       eval_set=[(X_test, y_test)],
                       early_stopping_rounds=50,
                       verbose=False)

        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        self.save_model()

        preds = self.model.predict(X_test)
        rmse = mean_squared_error(y_test, preds, squared=False)
        print(f"Model for {self.symbol} trained. RMSE: {rmse}")

    def predict(self, data):
        """
        Make a prediction for the next day's return.
        """
        if not os.path.exists(self.model_path):
            print("Model not found. Please train the model first.")
            return None

        self.load_model()

        # This is a simplification. In a real scenario, you'd need to make
        # sure all the features are correctly calculated for the prediction
        # point. For now, we'll just use the last available features.
        X, _ = self._prepare_data(data)

        if X.empty:
            return None

        return self.model.predict(X.tail(1))[0]

    def save_model(self):
        joblib.dump(self.model, self.model_path)
        print(f"Model saved to {self.model_path}")

    def load_model(self):
        self.model = joblib.load(self.model_path)
        print(f"Model loaded from {self.model_path}")
