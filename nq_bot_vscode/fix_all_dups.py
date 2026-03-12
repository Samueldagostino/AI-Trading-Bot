import ast

while True:
    data = open('Broker/order_manager.py','r',encoding='utf-8').read()
    try:
        ast.parse(data)
        print('File is clean - no syntax errors')
        break
    except SyntaxError as e:
        lines = data.split('\n')
        # Find the unclosed triple quote by counting
        count = 0
        problem = None
        for i, l in enumerate(lines):
            n = l.count('\"\"\"')
            for _ in range(n):
                count += 1
                if count % 2 == 1:
                    problem = i
        if problem is None:
            print('Could not find problem')
            break
        # Look for duplicate docstring near problem line
        found = False
        for i in range(max(0, problem-5), min(len(lines)-3, problem+5)):
            if '\"\"\"' in lines[i] and '\"\"\"' in lines[i+1]:
                print(f'Removing duplicate at line {i+2}: {lines[i+1].strip()}')
                del lines[i+1]
                found = True
                break
            if '\"\"\"' in lines[i] and '\"\"\"' in lines[i+3]:
                chunk = lines[i:i+4]
                after = lines[i+4:i+8]
                chunk_text = ''.join(chunk).strip()
                after_text = ''.join(after).strip()
                if chunk_text == after_text:
                    print(f'Removing duplicate block at lines {i+5}-{i+8}')
                    del lines[i+4:i+8]
                    found = True
                    break
        if not found:
            print(f'Cannot auto-fix near line {problem+1}')
            print(f'Context:')
            for j in range(max(0,problem-3), min(len(lines), problem+5)):
                print(f'  {j+1}: {lines[j]}')
            break
        open('Broker/order_manager.py','w',encoding='utf-8').write('\n'.join(lines))
