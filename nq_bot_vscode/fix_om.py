import subprocess
result = subprocess.run(['git', 'log', '--oneline', '-20', '--', 'Broker/order_manager.py'], capture_output=True, text=True)
print(result.stdout)
