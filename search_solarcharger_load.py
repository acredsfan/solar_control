import openpyxl, re
wb=openpyxl.load_workbook('CCGX-Modbus-TCP-register-list-3.60.xlsx', data_only=True)
pat1=re.compile('solarcharger', re.I)
pat2=re.compile('load', re.I)
rows=[]
for ws in wb.worksheets:
    for r in ws.iter_rows(values_only=True):
        if not r: continue
        cells=[str(c) for c in r if c is not None]
        joined=' '.join(cells)
        if pat1.search(joined) and pat2.search(joined):
            rows.append(cells)
print('TOTAL MATCHES', len(rows))
for r in rows[:50]:
    print(r)
