data = open('Broker/order_manager.py','r',encoding='utf-8').read()
data = data.replace('MAX_CONTRACTS = 2', 'MAX_CONTRACTS = 4')
open('Broker/order_manager.py','w',encoding='utf-8').write(data)
print('Done')
