lines = open('Broker/order_manager.py','r',encoding='utf-8').readlines()
print(f'Before: {len(lines)} lines')
print(f'Line 1109: {lines[1108].rstrip()}')
print(f'Line 1110: {lines[1109].rstrip()}')
print(f'Line 1111: {lines[1110].rstrip()}')
print(f'Line 1112: {lines[1111].rstrip()}')
print(f'Line 1113: {lines[1112].rstrip()}')
# Delete lines 1109-1112 (the duplicate docstring)
del lines[1108:1112]
# Fix MAX_CONTRACTS
data = ''.join(lines)
data = data.replace('MAX_CONTRACTS = 2', 'MAX_CONTRACTS = 4')
open('Broker/order_manager.py','w',encoding='utf-8').write(data)
print(f'After: {len(data.splitlines())} lines')
import ast
try:
    ast.parse(data)
    print('SYNTAX CHECK: PASSED')
except SyntaxError as e:
    print(f'SYNTAX ERROR at line {e.lineno}: {e.msg}')
