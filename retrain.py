import subprocess
import schedule
import time

def retrain():
    print("Retraining XGBoost model with latest data...")
    subprocess.run(['python3', 'data_fetcher.py'])
    subprocess.run(['python3', 'feature_engineer.py'])
    subprocess.run(['python3', 'label_creator.py'])
    subprocess.run(['python3', 'xgboost_trainer.py'])
    print("Retraining complete! New model saved.")

# Retrain every 24 hours
schedule.every(24).hours.do(retrain)

print("Retrainer running... will retrain every 24 hours")
while True:
    schedule.run_pending()
    time.sleep(60)


