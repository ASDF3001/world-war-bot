import re

with open('cogs/admin.py', 'r') as f:
    content = f.read()

# Add group definitions at the top of the class
class_def = 'class AdminCog(commands.Cog):'
group_defs = """class AdminCog(commands.Cog):
    op_group = app_commands.Group(name="op", description="運営・管理者用コマンド")
"""
content = content.replace(class_def, group_defs)

# Replace commands
content = re.sub(r'@app_commands\.command\(', r'@op_group.command(', content)

with open('cogs/admin.py', 'w') as f:
    f.write(content)
