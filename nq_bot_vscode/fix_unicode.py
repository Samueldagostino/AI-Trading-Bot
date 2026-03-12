import os
for root, dirs, files in os.walk('.'):
    for f in files:
        if f.endswith('.py'):
            path = os.path.join(root, f)
            data = open(path, 'r', encoding='utf-8').read()
            orig = data
            for bad, good in [(chr(0x2014),'--'),(chr(0x2013),'-'),(chr(0x2018),chr(39)),(chr(0x2019),chr(39)),(chr(0x201c),chr(34)),(chr(0x201d),chr(34))]:
                data = data.replace(bad, good)
            if data != orig:
                open(path, 'w', encoding='utf-8').write(data)
                print(f'Fixed: {path}')
print('Done')
