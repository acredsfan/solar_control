import openpyxl, json, re
wb=openpyxl.load_workbook('CCGX-Modbus-TCP-register-list-3.60.xlsx', data_only=True)
rows=[]
pattern=re.compile(r'load', re.IGNORECASE)
for ws in wb.worksheets:
    for r in ws.iter_rows(values_only=True):
        if not r: continue
        if any(isinstance(c,str) and pattern.search(c) for c in r):
            rows.append([c for c in r if c is not None])
print('WORKSHEETS', [w.title for w in wb.worksheets])
print('MATCHES', len(rows))
for row in rows[:60]:
    print(row)
