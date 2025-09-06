import os, yaml
exclude = set(os.environ.get('EXCLUDE_FOLDERS', '').split(','))
filters = {}
for entry in os.listdir('.'):
    if os.path.isdir(entry) and entry not in exclude:
        filters[entry] = [f"{entry}/**"]
with open('.github/filters.yml', 'w') as f:
    yaml.dump(filters, f, default_flow_style=False)