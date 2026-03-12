lines = open('Broker/order_manager.py','r',encoding='utf-8').readlines()
# Find and remove duplicate docstring blocks
i = 0
cleaned = []
while i < len(lines):
    if i < len(lines)-4 and '"""\n' in lines[i] and '"""\n' in lines[i+3]:
        text1 = lines[i:i+4]
        if i+4 < len(lines) and lines[i+4].strip().startswith('"""'):
            text2_start = i+4
            j = text2_start
            while j < len(lines) and not (j > text2_start and '"""' in lines[j]):
                j += 1
            if j < len(lines):
                cleaned.extend(lines[i:i+4])
                i = j + 1
                print(f'Removed duplicate docstring at line {text2_start+1}')
                continue
    cleaned.append(lines[i])
    i += 1
open('Broker/order_manager.py','w',encoding='utf-8').writelines(cleaned)
print('Done')
