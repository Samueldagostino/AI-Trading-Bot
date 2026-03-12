data = open('Broker/ibkr_client.py','r',encoding='utf-8').read()
if 'self.contract' not in data.split('def connect')[0][:500]:
    lines = open('Broker/ibkr_client.py','r',encoding='utf-8').readlines()
    for i,l in enumerate(lines):
        if 'Contract qualified' in l:
            print(f'Found qualification at line {i+1}: {l.strip()}')
        if 'self.contract' in l:
            print(f'self.contract at line {i+1}: {l.strip()}')
print('---')
import ast
try:
    ast.parse(data)
    print('ibkr_client.py syntax OK')
except SyntaxError as e:
    print(f'SYNTAX ERROR at line {e.lineno}: {e.msg}')
