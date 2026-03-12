lines = open('Broker/order_manager.py','r',encoding='utf-8').readlines()
del lines[1108:1112]
open('Broker/order_manager.py','w',encoding='utf-8').writelines(lines)
print('Deleted duplicate docstring lines 1109-1112')
