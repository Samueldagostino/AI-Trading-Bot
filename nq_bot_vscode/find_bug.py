import ast
try:
    ast.parse(open('Broker/order_manager.py','r',encoding='utf-8').read())
    print('No syntax errors')
except SyntaxError as e:
    print(f'Error at line {e.lineno}: {e.msg}')
    lines = open('Broker/order_manager.py','r',encoding='utf-8').readlines()
    count = 0
    for i,l in enumerate(lines):
        n = l.count('"""')
        count += n
        if count % 2 == 1 and n > 0:
            print(f'UNCLOSED at line {i+1}: {l.rstrip()}')
