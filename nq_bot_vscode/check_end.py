lines = open('Broker/order_manager.py','r',encoding='utf-8').readlines()
print(f'Total lines: {len(lines)}')
for i in range(len(lines)-10, len(lines)):
    print(f'{i+1}: {lines[i].rstrip()}')
