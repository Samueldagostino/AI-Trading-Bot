data = open('Broker/order_manager.py','r',encoding='utf-8').read()
data = data.replace('MAX_CONTRACTS = 2', 'MAX_CONTRACTS = 4')
import ast
try:
    ast.parse(data)
    open('Broker/order_manager.py','w',encoding='utf-8').write(data)
    print('PASSED - file is clean')
except SyntaxError as e:
    print(f'FAILED at line {e.lineno}: {e.msg}')
