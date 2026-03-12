lines = open('Broker/order_manager.py','r',encoding='utf-8').readlines()
for i in range(1100, 1120):
    print(f'{i+1}: {lines[i].rstrip()}')
