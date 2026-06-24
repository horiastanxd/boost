import re

with open('bin/auto', 'r') as f:
    content = f.read()

# Replace variables
content = content.replace('AUTO_MODE="${AUTO_MODE:-friendly}"', 'AUTO_MODE="${AUTO_MODE:-dynamic}"')

# Remove lines related to old modes in switch cases
content = re.sub(r'^\s*calm\).*?^\s*summer\).*?^\s*active\).*?^\s*quiet\|off\|friendly\).*?$', '        quiet|off|dynamic) ;;', content, flags=re.MULTILINE | re.DOTALL)

# Replace print loop
content = content.replace('for mode in calm summer friendly active quiet off; do', 'for mode in dynamic creator quiet off; do')

# Replace friendly string
content = content.replace('friendly_profile()', 'friendly_profile()') # keep friendly_profile name
content = content.replace('show_config_friendly()', 'show_config_friendly()')

# Setup wizard
wizard_old = """        1) mode=calm ;;
        2) mode=summer ;;
        3) mode=friendly ;;
        4) mode=active ;;
        5) mode=quiet ;;
        *) echo "Unknown choice, using calm."; mode=calm ;;"""
wizard_new = """        1) mode=dynamic ;;
        2) mode=creator ;;
        3) mode=quiet ;;
        *) echo "Unknown choice, using dynamic."; mode=dynamic ;;"""
content = content.replace(wizard_old, wizard_new)

# Fix available list
content = content.replace('Available: calm, summer, friendly, active, quiet, off, custom', 'Available: dynamic, creator, quiet, off, custom')

# Write back
with open('bin/auto', 'w') as f:
    f.write(content)

