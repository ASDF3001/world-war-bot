import re

with open('cogs/commands.py', 'r') as f:
    content = f.read()

# Replace UN commands
content = re.sub(r'@app_commands\.command\(name="invite_un",', r'@un_group.command(name="invite",', content)
content = re.sub(r'async def cmd_invite_un', r'async def cmd_un_invite', content)

content = re.sub(r'@app_commands\.command\(name="join_un",', r'@un_group.command(name="join",', content)
content = re.sub(r'async def cmd_join_un', r'async def cmd_un_join', content)

content = re.sub(r'@app_commands\.command\(name="leave_un",', r'@un_group.command(name="leave",', content)
content = re.sub(r'async def cmd_leave_un', r'async def cmd_un_leave', content)

content = re.sub(r'@app_commands\.command\(name="un_list",', r'@un_group.command(name="list",', content)
content = re.sub(r'async def cmd_un_list', r'async def cmd_un_list_group', content)

# Replace Camp commands
content = re.sub(r'@app_commands\.command\(name="create_camp",', r'@camp_group.command(name="create",', content)
content = re.sub(r'async def cmd_create_camp', r'async def cmd_camp_create', content)

content = re.sub(r'@app_commands\.command\(name="invite_camp",', r'@camp_group.command(name="invite",', content)
content = re.sub(r'async def cmd_invite_camp', r'async def cmd_camp_invite', content)

content = re.sub(r'@app_commands\.command\(name="join_camp",', r'@camp_group.command(name="join",', content)
content = re.sub(r'async def cmd_join_camp', r'async def cmd_camp_join', content)

content = re.sub(r'@app_commands\.command\(name="camp_list",', r'@camp_group.command(name="list",', content)
content = re.sub(r'async def cmd_camp_list', r'async def cmd_camp_list_group', content)

# Remove the duplicate invite command
content = re.sub(r'@app_commands\.command\(name="invite", description="指定したプレイヤーを陣営に招待します \(/invite_camp と同じ\)"\)\s*async def cmd_invite_alias\(self, interaction: discord.Interaction, target_user: discord.Member\):\s*await self\.cmd_camp_invite\(interaction, target_user\)', '', content, flags=re.MULTILINE)

# Add group definitions at the top of the class
class_def = 'class CommandsCog(commands.Cog):'
group_defs = """class CommandsCog(commands.Cog):
    un_group = app_commands.Group(name="un", description="国連(UN)に関するコマンド")
    camp_group = app_commands.Group(name="camp", description="陣営(Camp)に関するコマンド")
    trophy_group = app_commands.Group(name="trophy", description="称号・トロフィーに関するコマンド")
"""
content = content.replace(class_def, group_defs)

# Replace Trophy commands
content = re.sub(r'@app_commands\.command\(name="trophy",', r'@trophy_group.command(name="show",', content)
content = re.sub(r'@app_commands\.command\(name="trophy_equip",', r'@trophy_group.command(name="equip",', content)


with open('cogs/commands.py', 'w') as f:
    f.write(content)
