import discord
from discord import app_commands
import random
import datetime
import asyncio
import os

from main import (
    get_db_connection, safe_defer, send_dm_fallback, ensure_world_context, 
    is_oil_enabled, resolve_country_code, is_allied, is_at_war, 
    check_and_create_user, _generate_current_map_sync, get_promo_and_tip, 
    VALID_CODES, ADJACENCY_GRAPH, DEFAULT_DEFENSE, BASE_INCOME, TERRITORY_YIELD,
    add_world_log, add_trophy, is_peace_treaty_active
)

work_cooldowns = {}

async def my_countries_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return []
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT iso_alpha FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        choices = [row[0] for row in c.fetchall() if current.lower() in row[0].lower()]
        return [app_commands.Choice(name=f"領土: {code}", value=code) for code in choices[:25]]

# ==============================================================================
# UI・画面パーツ (Views / Modals)
# ==============================================================================
class StatsView(discord.ui.View):
    def __init__(self, author_id: int, user_data: dict):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.user_data = user_data

    def generate_embed(self, category: str):
        data = self.user_data
        embed = discord.Embed(title=f"📊 {data['user_name']} ({data['country_name']}) の国家データ", color=0xf1c40f)
        
        if category == "economy":
            embed.description = "**💰 経済・内政**"
            embed.add_field(name="保有資金 (Gold)", value=f"{data['money']}", inline=True)
            embed.add_field(name="備蓄石油 (Oil)", value=f"{data['oil']} L", inline=True)
            embed.add_field(name="戦債 (借金)", value=f"{data['debt']}", inline=True)
            embed.add_field(name="総投資額", value=f"{data['invest']}", inline=True)
            embed.add_field(name="テックレベル", value=f"Lv. {data['tech_level']}", inline=True)
        elif category == "military":
            embed.description = "**⚔️ 軍事・領土**"
            embed.add_field(name="支配領土数", value=f"{data['territory']} カ国", inline=True)
            embed.add_field(name="総防衛力", value=f"{data['defense']} 人", inline=True)
            embed.add_field(name="戦績", value=f"勝: {data['wins']} / 敗: {data['losses']}", inline=True)
        elif category == "profile":
            embed.description = "**🏅 プロフィール・実績**"
            embed.add_field(name="称号", value=f"{data['title']}", inline=True)
            embed.add_field(name="トロフィー", value=f"{data['trophy_count']} 個", inline=True)
            
        return embed

    @discord.ui.button(label="経済・内政", style=discord.ButtonStyle.success)
    async def btn_eco(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id: return await interaction.response.send_message("操作できません。", ephemeral=True)
        await interaction.response.edit_message(embed=self.generate_embed("economy"), view=self)

    @discord.ui.button(label="軍事・領土", style=discord.ButtonStyle.danger)
    async def btn_mil(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id: return await interaction.response.send_message("操作できません。", ephemeral=True)
        await interaction.response.edit_message(embed=self.generate_embed("military"), view=self)

    @discord.ui.button(label="実績・プロフ", style=discord.ButtonStyle.primary)
    async def btn_prof(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id: return await interaction.response.send_message("操作できません。", ephemeral=True)
        await interaction.response.edit_message(embed=self.generate_embed("profile"), view=self)

class InvestModal(discord.ui.Modal, title="国家予算の投資"):
    amount_input = discord.ui.TextInput(label="投資額 (Gold)", placeholder="例: 1000", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        try: amount = int(self.amount_input.value)
        except ValueError: return await interaction.response.send_message("[エラー] 数字を入力してください。", ephemeral=True)
        await run_invest(interaction, amount)

class SpyModal(discord.ui.Modal, title="スパイの派遣 (費用: 1500 Gold)"):
    target_input = discord.ui.TextInput(label="潜入先の国コード", placeholder="例: JPN", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        await run_spy(interaction, self.target_input.value)

class PropagandaModal(discord.ui.Modal, title="プロパガンダ工作 (費用: 3000 Gold)"):
    target_input = discord.ui.TextInput(label="工作先の国コード", placeholder="例: JPN", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        await run_propaganda(interaction, self.target_input.value)
class OilImportView(discord.ui.View):
    def __init__(self, guild_id, world_id, buyer_id, seller_id, amount, price):
        super().__init__(timeout=86400)
        self.guild_id = guild_id; self.world_id = world_id; self.buyer_id = buyer_id
        self.seller_id = seller_id; self.amount = amount; self.price = price

    @discord.ui.button(label="許可する", style=discord.ButtonStyle.success)
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.seller_id: return await interaction.response.send_message("[エラー] 権限がありません。", ephemeral=True)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT oil FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (self.guild_id, self.world_id, self.seller_id))
            seller_row = c.fetchone()
            if not seller_row or seller_row[0] < self.amount: return await interaction.response.send_message("[エラー] オイル不足です。", ephemeral=True)
            c.execute("SELECT gold FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (self.guild_id, self.world_id, self.buyer_id))
            buyer_row = c.fetchone()
            if not buyer_row or buyer_row[0] < self.price: return await interaction.response.send_message("[エラー] 相手の資金不足です。", ephemeral=True)

            c.execute("UPDATE players SET oil=oil-?, gold=gold+? WHERE guild_id=? AND world_id=? AND user_id=?", (self.amount, self.price, self.guild_id, self.world_id, self.seller_id))
            c.execute("UPDATE players SET oil=oil+?, gold=gold-? WHERE guild_id=? AND world_id=? AND user_id=?", (self.amount, self.price, self.guild_id, self.world_id, self.buyer_id))
            conn.commit()

        for child in self.children: child.disabled = True
        await interaction.response.edit_message(content=f"[取引成立 / 世界#{self.world_id}]\n<@{self.buyer_id}> へ **{self.amount} L** を輸出し **{self.price} Gold** を受け取りました。", view=self)

        guild = interaction.client.get_guild(int(self.guild_id))
        if guild:
            buyer_member = guild.get_member(int(self.buyer_id))
            if buyer_member:
                try: await buyer_member.send(f"[取引完了 / 世界#{self.world_id}]\n<@{self.seller_id}> が申請を許可しました。\n**{self.amount} L** を獲得し、**{self.price} Gold** 支払いました。")
                except: pass

    @discord.ui.button(label="拒否する", style=discord.ButtonStyle.danger)
    async def btn_reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.seller_id: return await interaction.response.send_message("[エラー] 権限がありません。", ephemeral=True)
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(content=f"[拒否 / 世界#{self.world_id}] 取引を拒否しました。", view=self)

class DiplomacyUserSelect(discord.ui.UserSelect):
    def __init__(self, action: str, world_id: int):
        self.action = action; self.world_id = world_id
        super().__init__(placeholder="対象のプレイヤーを選択してください...")

    async def callback(self, interaction: discord.Interaction):
        await safe_defer(interaction) 
        guild_id, user_id, target_id = str(interaction.guild_id), str(interaction.user.id), str(self.values[0].id)
        w_id = self.world_id; target = self.values[0]

        if user_id == target_id: return await interaction.followup.send("[エラー] 自分は選択できません。", ephemeral=True)
        if target.bot: return await interaction.followup.send("[エラー] Botは選択できません。", ephemeral=True)

        target_member = interaction.guild.get_member(int(target_id))

        with get_db_connection() as conn:
            c = conn.cursor()
            is_ally = is_allied(guild_id, w_id, user_id, target_id)

            if self.action == "war":
                if is_ally: return await interaction.followup.send("[エラー] 同盟国には宣戦布告できません。", ephemeral=True)
                view = discord.ui.View(timeout=86400)
                btn_accept = discord.ui.Button(label="受諾する", style=discord.ButtonStyle.danger)
                btn_reject = discord.ui.Button(label="拒否する", style=discord.ButtonStyle.success)

                async def accept_callback(i: discord.Interaction):
                    if str(i.user.id) != target_id: return await i.response.send_message("[エラー] 権限がありません。", ephemeral=True)
                    with get_db_connection() as conn2:
                        c2 = conn2.cursor()
                        c2.execute("INSERT OR IGNORE INTO wars (guild_id, world_id, attacker_id, defender_id) VALUES (?, ?, ?, ?)", (guild_id, w_id, user_id, target_id))
                        conn2.commit()
                    for child in view.children: child.disabled = True
                    await i.response.edit_message(content=f"[宣戦布告 受諾 / 世界#{w_id}]\n<@{target_id}> と <@{user_id}> は正式に戦争状態に突入しました！", view=view)

                async def reject_callback(i: discord.Interaction):
                    if str(i.user.id) != target_id: return await i.response.send_message("[エラー] 権限がありません。", ephemeral=True)
                    for child in view.children: child.disabled = True
                    await i.response.edit_message(content=f"[宣戦布告 拒否 / 世界#{w_id}]\n<@{target_id}> は宣戦布告を拒否しました。\n<@{user_id}> は強行軍（奇襲）を仕掛けるしかありません。", view=view)

                btn_accept.callback = accept_callback; btn_reject.callback = reject_callback
                view.add_item(btn_accept); view.add_item(btn_reject)

                content = f"**[宣戦布告の使者 / 世界#{w_id}]**\n<@{user_id}> から宣戦布告の使者が到着しました！\n受諾するか拒否するか選択してください。\n※拒否しても交渉決裂となり、相手は強行軍(奇襲)を仕掛けることができます。"
                if target_member: await send_dm_fallback(target_member, interaction.channel, content, view)
                else: await interaction.channel.send(f"<@{target_id}> {content}", view=view)
                await interaction.followup.send("[完了] 宣戦布告の使者を派遣しました。", ephemeral=True)

            elif self.action == "alliance_invite":
                if is_ally: return await interaction.followup.send("[エラー] すでに同盟を結んでいます。", ephemeral=True)
                view = discord.ui.View(timeout=86400)
                btn_accept = discord.ui.Button(label="承認する", style=discord.ButtonStyle.success)
                async def accept_callback(i: discord.Interaction):
                    if str(i.user.id) != target_id: return await i.response.send_message("[エラー] 権限がありません。", ephemeral=True)
                    with get_db_connection() as conn2:
                        c2 = conn2.cursor()
                        c2.execute("INSERT OR IGNORE INTO alliances (guild_id, world_id, user_a, user_b) VALUES (?, ?, ?, ?)", (guild_id, w_id, user_id, target_id))
                        c2.execute("DELETE FROM wars WHERE guild_id=? AND world_id=? AND ((attacker_id=? AND defender_id=?) OR (attacker_id=? AND defender_id=?))", (guild_id, w_id, user_id, target_id, target_id, user_id))
                        conn2.commit()
                    for child in view.children: child.disabled = True
                    await i.response.edit_message(content=f"[同盟成立 / 世界#{w_id}]\n<@{user_id}> と <@{target_id}> が軍事同盟を結びました！", view=view)
                btn_accept.callback = accept_callback
                view.add_item(btn_accept)
                
                content = f"**[軍事同盟の提案 / 世界#{w_id}]**\n<@{user_id}> から軍事同盟の提案が届きました！\n承認を押して同盟を結びますか？"
                if target_member: await send_dm_fallback(target_member, interaction.channel, content, view)
                else: await interaction.channel.send(f"<@{target_id}> {content}", view=view)
                await interaction.followup.send("[完了] 同盟の提案を送信しました。", ephemeral=True)

            elif self.action == "alliance_cancel":
                if not is_ally: return await interaction.followup.send("[エラー] 同盟を結んでいません。", ephemeral=True)
                c.execute("DELETE FROM alliances WHERE guild_id=? AND world_id=? AND ((user_a=? AND user_b=?) OR (user_a=? AND user_b=?))", (guild_id, w_id, user_id, target_id, target_id, user_id))
                conn.commit()
                await interaction.channel.send(f"[同盟破棄 / 世界#{w_id}] <@{user_id}> が <@{target_id}> との同盟を破棄しました。")
                await interaction.followup.send("[完了] 同盟を破棄しました。", ephemeral=True)

class DiplomacyActionSelect(discord.ui.Select):
    def __init__(self, world_id: int):
        self.world_id = world_id
        options = [
            discord.SelectOption(label="宣戦布告", value="war"),
            discord.SelectOption(label="同盟の提案", value="alliance_invite"),
            discord.SelectOption(label="同盟の破棄", value="alliance_cancel"),
            discord.SelectOption(label="同盟国一覧", value="alliance_list"),
            discord.SelectOption(label="国連加盟国一覧", value="un_list"),
            discord.SelectOption(label="設立陣営一覧", value="camp_list")
        ]
        super().__init__(placeholder="実行する外交アクションを選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        if val == "alliance_list":
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT user_a, user_b FROM alliances WHERE guild_id=? AND world_id=?", (str(interaction.guild_id), self.world_id))
                rows = c.fetchall()
            if not rows: return await interaction.response.send_message(f"[世界#{self.world_id}] 結ばれている同盟はありません。", ephemeral=True)
            embed = discord.Embed(title=f"締結済みの軍事同盟 [世界#{self.world_id}]", description="\n".join([f"・ <@{a}> ＆ <@{b}>" for a, b in rows]), color=0x3498db)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        elif val == "un_list": await run_un_list(interaction)
        elif val == "camp_list": await run_camp_list(interaction)
        else:
            view = discord.ui.View(timeout=60)
            view.add_item(DiplomacyUserSelect(val, self.world_id))
            await interaction.response.send_message("👉 次に対象となるプレイヤーを選択してください:", view=view, ephemeral=True)

class CountryManageSelect(discord.ui.Select):
    def __init__(self, territories, world_id, user_id):
        self.world_id = world_id; self.user_id = user_id
        options = [discord.SelectOption(label=t[0], description=f"防衛力: {t[1]}") for t in territories[:25]]
        super().__init__(placeholder="管理する領土を選択してください", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_iso = self.values[0]
        view = discord.ui.View(timeout=60)
        btn_abandon = discord.ui.Button(label="所有権を放棄する", style=discord.ButtonStyle.danger)
        async def abandon_callback(i: discord.Interaction):
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute("DELETE FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=? AND owner_id=?", (str(i.guild_id), self.world_id, selected_iso, self.user_id))
                conn.commit()
            for child in view.children: child.disabled = True
            await i.response.edit_message(content=f"[完了] **{selected_iso}** の所有権を放棄しました。", view=view)
        btn_abandon.callback = abandon_callback
        view.add_item(btn_abandon)
        await interaction.response.edit_message(content=f"**{selected_iso}** の管理メニュー", view=view)

class CountryManageMainView(discord.ui.View):
    def __init__(self, territories, world_id, user_id):
        super().__init__(timeout=60)
        self.add_item(CountryManageSelect(territories, world_id, user_id))

class HelpView(discord.ui.View):
    def __init__(self): 
        super().__init__(timeout=None)

    def generate_embed(self, page_id: str):
        embed = discord.Embed(title="全世界戦争Bot ガイドブック", color=0x3498db)
        
        if page_id == "basic":
            embed.description = "**基本ルールと資源**\nこのBotは国を奪い合い、世界の覇権を握るシミュレーションゲームです。"
            embed.add_field(name="定時給付", value="毎日(7:00 / 19:00 JST)に基本給(1000G)・税収・石油配給があります。維持費(領土数×25L)も引かれます。", inline=False)
            embed.add_field(name="労働と資源", value="`/work` を使うと資金とオイルを稼げます。序盤はこれを回しましょう。\n資金が尽きたら `/war_bonds` で国庫から5000G借りられます(給付から天引き)。", inline=False)
            
        elif page_id == "war":
            embed.description = "**戦争と内政**"
            embed.add_field(name="侵攻作戦", value="`/attack` で他国や空き地を攻めます。**入力した資金がそのまま消費**されます。\n宣戦布告なしの奇襲や、遠い国への攻撃は、実質的な攻撃力がペナルティで減少します。", inline=False)
            embed.add_field(name="防衛と大国ボーナス", value="`/defend` で領土の防衛力を上げます。\n**領土を3つ以上持つ大国**は、防衛費が割引(0.9倍)され、より効率的に国を守ることができます。", inline=False)
            
        elif page_id == "diplomacy":
            embed.description = "**外交と貿易**"
            embed.add_field(name="宣戦布告と同盟", value="`/gui` から行います。宣戦布告を断られても、相手に奇襲を仕掛けることができます。\n同盟を結ぶと相互不可侵となり、攻撃を受けた際にDM通知が飛びます。", inline=False)
            embed.add_field(name="貿易", value="`/import_oil` で他プレイヤーからオイルを購入したり、`/convert_resource` でシステムと直接資金・オイルを交換できます。", inline=False)
            
        elif page_id == "system":
            embed.description = "**システムとアップデート**"
            embed.add_field(name="バージョン一覧", value="`/version` : 閲覧可能なアップデート一覧（最新10件）を確認できます。", inline=False)
            embed.add_field(name="アプデ詳細確認", value="`/update` : 最新のアップデート情報を表示します。\n`/update [バージョン]` : 指定した過去バージョン（例: `/update v1.0`）の詳細を表示します。", inline=False)
            embed.add_field(name="個人設定", value="`/user_setting` : 確認ダイアログのON/OFFなど、各種表示設定を変更できます。", inline=False)

        elif page_id == "admin":
            embed.description = "**管理者向け機能**"
            embed.add_field(name="OPコマンド", value="初めに `/op setup` で専用チャンネルを一括作成してください。\nその他の設定もすべて `/op` から始まるコマンドで操作可能です。", inline=False)
            
        return embed

    @discord.ui.button(label="基本ルール", style=discord.ButtonStyle.primary, custom_id="help_basic")
    async def btn_basic(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.generate_embed("basic"), view=self)

    @discord.ui.button(label="戦争と内政", style=discord.ButtonStyle.secondary, custom_id="help_war")
    async def btn_war(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.generate_embed("war"), view=self)

    @discord.ui.button(label="外交と貿易", style=discord.ButtonStyle.secondary, custom_id="help_dip")
    async def btn_dip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.generate_embed("diplomacy"), view=self)

    @discord.ui.button(label="システム・更新", style=discord.ButtonStyle.success, custom_id="help_sys")
    async def btn_sys(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.generate_embed("system"), view=self)

    @discord.ui.button(label="管理者向け", style=discord.ButtonStyle.danger, custom_id="help_admin")
    async def btn_admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.generate_embed("admin"), view=self)

class CommandGUIView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.select(placeholder="メニューからかんたん操作を選択...", options=[
        discord.SelectOption(label="ステータス確認", value="status", description="自分の資金・石油や総兵力をチェック"),
        discord.SelectOption(label="侵攻作戦の立案 (Attack)", value="attack", description="指定した国へ攻撃を仕掛ける"),
        discord.SelectOption(label="外交手続き (GUI)", value="gui", description="宣戦布告や同盟の申請/破棄を行う"),
        discord.SelectOption(label="領土の管理", value="country_management", description="所有している領土の所有権放棄メニュー"),
        discord.SelectOption(label="国家への投資 (Invest)", value="invest", description="資金を投資して国力を高める"),
        discord.SelectOption(label="技術研究 (Research)", value="research", description="技術レベルを上げ、工作の成功率をアップ"),
        discord.SelectOption(label="スパイ派遣 (Spy)", value="spy", description="他国へ潜入し、情報を抜き取る"),
        discord.SelectOption(label="プロパガンダ (Propaganda)", value="propaganda", description="他国の防衛力を低下させる"),
    ])
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        val = select.values[0]
        if val == "attack": await interaction.response.send_modal(AttackTargetModal())
        elif val == "invest": await interaction.response.send_modal(InvestModal())
        elif val == "spy": await interaction.response.send_modal(SpyModal())
        elif val == "propaganda": await interaction.response.send_modal(PropagandaModal())
        else:
            await interaction.response.edit_message(view=self)
            if val == "status": await run_status(interaction)
            elif val == "gui": await run_diplomacy(interaction)
            elif val == "country_management": await run_country_management(interaction)
            elif val == "research": await run_research(interaction)

class AttackConfirmView(discord.ui.View):
    def __init__(self, target_code: str, active_world: int, total_cost: int, actual_power: int, warning_text: str):
        super().__init__(timeout=60)
        self.target_code = target_code; self.active_world = active_world
        self.total_cost = total_cost; self.actual_power = actual_power; self.warning_text = warning_text

    @discord.ui.button(label="この内容で進軍する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(content="部隊を出撃させています...", view=self)
        await execute_attack_logic(interaction, self.target_code, self.active_world, self.total_cost)

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(content="作戦を中止しました。", view=self)

class TroopModal(discord.ui.Modal, title="攻撃部隊の編成"):
    troops_input = discord.ui.TextInput(label="投入する資金 (消費額)", placeholder="例: 1000", required=True)
    def __init__(self, target_code: str, active_world: int):
        super().__init__()
        self.target_code = target_code; self.active_world = active_world

    async def on_submit(self, interaction: discord.Interaction):
        raw_val = self.troops_input.value.translate(str.maketrans('０１２３４５６７８９', '0123456789')).replace(',', '').strip()
        try: total_cost = int(raw_val)
        except ValueError: return await interaction.response.send_message("[エラー] 正しい数字を入力してください。", ephemeral=True)
        if total_cost <= 0: return await interaction.response.send_message("[エラー] 資金は1以上で入力してください。", ephemeral=True)

        guild_id, user_id, world_id = str(interaction.guild_id), str(interaction.user.id), self.active_world
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT confirm_attack FROM user_settings WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            row = c.fetchone(); confirm = row[0] if row else 1
            
            c.execute("SELECT owner_id FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, self.target_code))
            def_row = c.fetchone(); defender_id = def_row[0] if def_row else None
            
            base_cost_multiplier = 1.0
            if defender_id and not is_at_war(guild_id, world_id, user_id, defender_id):
                base_cost_multiplier = 1.5
            
            distance_penalty = 1.0
            c.execute("SELECT adjacency_penalty FROM server_channels WHERE guild_id=?", (guild_id,))
            row_adj = c.fetchone()
            if row_adj and row_adj[0] == 1:
                c.execute("SELECT iso_alpha FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
                my_lands = [r[0] for r in c.fetchall()]
                if my_lands and not any(self.target_code in ADJACENCY_GRAPH.get(l, set()) for l in my_lands):
                    distance_penalty = 1.5

            final_cost_multiplier = base_cost_multiplier * distance_penalty
            actual_power = int(total_cost / final_cost_multiplier)

        if confirm == 1:
            warning = []
            if base_cost_multiplier > 1.0: warning.append("[警告] 未宣戦による奇襲ペナルティあり")
            if distance_penalty > 1.0: warning.append("[警告] 非隣接による遠征ペナルティあり")
            warn_str = "\n".join(warning) if warning else "[情報] ペナルティなし"

            view = AttackConfirmView(self.target_code, world_id, total_cost, actual_power, warn_str)
            msg = f"**最終確認**\n対象: **{self.target_code}**\n\n投入資金: **{total_cost} Gold**\n{warn_str}\n\n実質的な攻撃力: **{actual_power}**\nこの内容で出撃してよろしいですか？"
            await interaction.response.send_message(msg, view=view, ephemeral=True)
        else:
            await safe_defer(interaction)
            await execute_attack_logic(interaction, self.target_code, world_id, total_cost)

class AttackView(discord.ui.View):
    def __init__(self, author_id: int, target_code: str, active_world: int):
        super().__init__(timeout=60)
        self.author_id = author_id; self.target_code = target_code; self.active_world = active_world

    @discord.ui.button(label="進軍する", style=discord.ButtonStyle.danger)
    async def confirm_attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id: return await interaction.response.send_message("[エラー] 操作できません。", ephemeral=True)
        await interaction.response.send_modal(TroopModal(self.target_code, self.active_world))
        for child in self.children: child.disabled = True
        try: await interaction.message.edit(view=self)
        except Exception: pass

    @discord.ui.button(label="取り消す", style=discord.ButtonStyle.secondary)
    async def cancel_attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id: return await interaction.response.send_message("[エラー] 操作できません。", ephemeral=True)
        for child in self.children: child.disabled = True
        try: await interaction.message.edit(content="作戦を中止しました。", embed=None, view=self)
        except Exception: pass

class AttackTargetModal(discord.ui.Modal, title="侵攻先の設定"):
    target_input = discord.ui.TextInput(label="攻撃目標の国コード", placeholder="例: JPN", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        await run_attack(interaction, self.target_input.value)

class DefendAmountModal(discord.ui.Modal, title="防衛費の投資"):
    amount_input = discord.ui.TextInput(label="1国あたりの投資額", placeholder="例: 100", required=True)
    def __init__(self, selected_codes, active_world):
        super().__init__()
        self.selected_codes = selected_codes; self.active_world = active_world
    async def on_submit(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        try: amount = int(self.amount_input.value)
        except ValueError: return await interaction.followup.send("[エラー] 正しい数字を入力してください。", ephemeral=True)
        await execute_defend_logic(interaction, self.selected_codes, amount, self.active_world)

class DefendMultiSelect(discord.ui.Select):
    def __init__(self, territories, active_world):
        self.active_world = active_world
        options = [discord.SelectOption(label=t[0], description=f"現在の防衛力: {t[1]}") for t in territories[:25]]
        super().__init__(placeholder="投資する領土を選択 (複数可)", min_values=1, max_values=len(options), options=options)
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DefendAmountModal(self.values, self.active_world))

class DefendView(discord.ui.View):
    def __init__(self, territories, active_world):
        super().__init__(timeout=60)
        self.add_item(DefendMultiSelect(territories, active_world))

# ==============================================================================
# ロジック・ゲーム処理
# ==============================================================================
async def execute_attack_logic(interaction: discord.Interaction, target_code: str, active_world: int, total_cost: int):
    guild_id, user_id, user_name, world_id = str(interaction.guild_id), str(interaction.user.id), interaction.user.display_name, active_world
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT gold, oil FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        p_row = c.fetchone()
        current_gold, current_oil = (p_row[0], p_row[1]) if p_row else (0, 0)
        
        oil_enabled = is_oil_enabled(guild_id, world_id)
        oil_cost = total_cost if oil_enabled else 0
        if oil_enabled and current_oil < oil_cost: return await interaction.followup.send(f"[エラー] 石油が不足しています！(必要: {oil_cost} L / 現在: {current_oil} L)", ephemeral=True)

        c.execute("SELECT owner_id, defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, target_code))
        def_row = c.fetchone()
        defender_id, base_def = (def_row[0], def_row[1]) if def_row else (None, DEFAULT_DEFENSE)

        if defender_id and is_allied(guild_id, world_id, user_id, defender_id): return await interaction.followup.send("[エラー] 同盟国の領土は攻撃できません！", ephemeral=True)
        if defender_id and is_peace_treaty_active(guild_id, world_id, user_id, defender_id): return await interaction.followup.send("[エラー] このプレイヤーとは不戦協定(Peace Treaty)を結んでいます！期間中は攻撃できません。", ephemeral=True)

        war_status_text, defense_power, base_cost_multiplier = "正規の戦争", base_def, 1.0
        if defender_id and not is_at_war(guild_id, world_id, user_id, defender_id):
            war_status_text, defense_power, base_cost_multiplier = "[警告] 奇襲/強行軍 (実質攻撃力が低下します)", int(base_def * 1.5), 1.5
        
        distance_penalty, distance_text = 1.0, ""
        c.execute("SELECT adjacency_penalty FROM server_channels WHERE guild_id=?", (guild_id,))
        row_adj = c.fetchone()
        if row_adj and row_adj[0] == 1:
            c.execute("SELECT iso_alpha FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
            my_lands = [r[0] for r in c.fetchall()]
            if my_lands and not any(target_code in ADJACENCY_GRAPH.get(l, set()) for l in my_lands):
                distance_penalty = 1.5
                distance_text = "\n[警告] 遠征ペナルティ (実質攻撃力が低下します)"

        final_cost_multiplier = base_cost_multiplier * distance_penalty
        actual_power = int(total_cost / final_cost_multiplier)

        if current_gold < total_cost: return await interaction.followup.send(f"[エラー] 資金不足です。(現在: {current_gold} / 指定額: {total_cost} gold)", ephemeral=True)

        atk_roll, def_roll = int(actual_power * random.uniform(0.8, 1.2)), int(defense_power * random.uniform(0.8, 1.2))
        embed = discord.Embed(title=f"作戦報告: {target_code} 侵攻 [世界#{world_id}]")
        
        if atk_roll > def_roll:
            surviving_troops = max(10, actual_power - int(actual_power * random.uniform(0.1, 0.4)))
            c.execute("UPDATE players SET gold=?, oil=? WHERE guild_id=? AND world_id=? AND user_id=?", (current_gold - total_cost, current_oil - oil_cost, guild_id, world_id, user_id))
            c.execute("INSERT OR REPLACE INTO territories (guild_id, world_id, iso_alpha, owner_id, defense) VALUES (?, ?, ?, ?, ?)", (guild_id, world_id, target_code, user_id, surviving_troops))
            embed.color = 0x2ecc71
            embed.description = f"**作戦成功！ {user_name} が {target_code} を制圧しました！**\n\n作戦タイプ: {war_status_text}{distance_text}\n消費資金: **{total_cost}** Gold\n実質攻撃力: **{actual_power}** (ペナルティ適用後)\n残存配置兵力: {surviving_troops}人"
            add_world_log(guild_id, world_id, f"{user_name} が {target_code} への侵攻作戦に成功し、制圧しました。")
            add_trophy(guild_id, world_id, user_id, "新進気鋭の征服者")
        else:
            new_defense = max(10, base_def - int(atk_roll * random.uniform(0.4, 0.8)))
            c.execute("UPDATE players SET gold=?, oil=? WHERE guild_id=? AND world_id=? AND user_id=?", (current_gold - total_cost, current_oil - oil_cost, guild_id, world_id, user_id))
            if defender_id: c.execute("UPDATE territories SET defense=? WHERE guild_id=? AND world_id=? AND iso_alpha=?", (new_defense, guild_id, world_id, target_code))
            embed.color = 0xe74c3c
            embed.description = f"**作戦失敗... 防衛に阻まれ全滅しました。**\n\n作戦タイプ: {war_status_text}{distance_text}\n損失資金: **{total_cost}** Gold\n実質攻撃力: **{actual_power}**"
            add_world_log(guild_id, world_id, f"{user_name} の {target_code} への侵攻作戦は防衛軍に阻まれ失敗しました。")
        conn.commit()

    if defender_id:
        notify_users = set([defender_id])
        with get_db_connection() as conn3:
            c3 = conn3.cursor()
            c3.execute("SELECT user_a, user_b FROM alliances WHERE guild_id=? AND world_id=? AND (user_a=? OR user_b=?)", (guild_id, world_id, defender_id, defender_id))
            for ua, ub in c3.fetchall(): notify_users.add(ua); notify_users.add(ub)
        if user_id in notify_users: notify_users.remove(user_id)

        async def send_dms():
            guild = interaction.client.get_guild(int(guild_id))
            if not guild: return
            for uid in notify_users:
                member = guild.get_member(int(uid))
                if member and not member.bot:
                    try:
                        role_text = "あなたの領土" if uid == defender_id else f"同盟国 (<@{defender_id}>) の領土"
                        msg = f"[緊急事態発生 / 世界#{world_id}]\n{role_text} **【{target_code}】** が <@{user_id}> ({user_name}) から攻撃を受けました！至急状況を確認してください！"
                        await member.send(msg)
                    except Exception: pass
        interaction.client.loop.create_task(send_dms())

    map_file, _ = await asyncio.to_thread(_generate_current_map_sync, guild_id, world_id)
    content_msg = get_promo_and_tip()
    
    if map_file: await interaction.channel.send(content=content_msg, embed=embed.set_image(url="attachment://war_map.png"), file=map_file)
    else: await interaction.channel.send(content=content_msg, embed=embed)
    
    if interaction.response.is_done(): await interaction.followup.send("[完了] 作戦が完了しました。", ephemeral=True)
    else: await interaction.response.send_message("[完了] 作戦が完了しました。", ephemeral=True)

async def execute_defend_logic(interaction: discord.Interaction, target_codes: list[str], amount_per_country: int, world_id: int):
    if amount_per_country <= 0: return await interaction.followup.send("[エラー] 金額は1以上にしてください。", ephemeral=True)
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    total_cost = amount_per_country * len(target_codes)
    
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT gold FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        p_row = c.fetchone()
        if not p_row or p_row[0] < total_cost: return await interaction.followup.send(f"[エラー] 資金不足です。(必要: {total_cost} Gold)", ephemeral=True)
            
        c.execute("SELECT COUNT(*) FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        my_lands = c.fetchone()[0]
        is_empire = my_lands >= 3
        gained_defense = int(amount_per_country / 0.9) if is_empire else amount_per_country
        
        valid_targets = []
        for code in target_codes:
            c.execute("SELECT owner_id, defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, code))
            t_row = c.fetchone()
            if t_row and t_row[0] == user_id: valid_targets.append(code)
                
        if not valid_targets: return await interaction.followup.send("[エラー] 対象の領土が見つからないか、あなたの領土ではありません。", ephemeral=True)
            
        actual_total_cost = amount_per_country * len(valid_targets)
        c.execute("UPDATE players SET gold=gold-? WHERE guild_id=? AND world_id=? AND user_id=?", (actual_total_cost, guild_id, world_id, user_id))
        for code in valid_targets:
            c.execute("UPDATE territories SET defense=defense+? WHERE guild_id=? AND world_id=? AND iso_alpha=?", (gained_defense, guild_id, world_id, code))
        conn.commit()
    
    empire_msg = "\n[大国ボーナス] 防衛コスト効率が上昇しました。" if is_empire else ""
    msg = f"[完了] {', '.join(valid_targets)} に各 {amount_per_country} Gold 投資しました。(増加防衛力: +{gained_defense}){empire_msg}"
    await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)

async def run_status(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    with get_db_connection() as conn:
        c = conn.cursor()
        check_and_create_user(c, guild_id, world_id, user_id, interaction.user.display_name)
        c.execute("SELECT gold, oil, main_country, debt FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        row = c.fetchone()
        gold, oil, main_country, debt = row[0], row[1], row[2], row[3] if row else (0,0,"",0)
        c.execute("SELECT iso_alpha, defense FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        territories = c.fetchall()
    
    tax = sum(TERRITORY_YIELD.get(t[0], 50) for t in territories)
    oil_drain = len(territories) * 25
    oil_status = f"[システム有効]\n(次回: 配給2000 - 維持費{oil_drain} = **{2000 - oil_drain} L**)" if is_oil_enabled(guild_id, world_id) else "[無効]"

    embed = discord.Embed(title=f"国家ステータス [世界#{world_id}]", color=0xf1c40f)
    embed.add_field(name="メイン国家", value=f"**{main_country}**")
    embed.add_field(name="保有資金", value=f"**{gold}** Gold")
    embed.add_field(name="備蓄石油", value=f"**{oil}** L\n{oil_status}")
    embed.add_field(name="総兵力 (合計防衛力)", value=f"**{sum(t[1] for t in territories)}** 人")
    embed.add_field(name="次回の定時資金収入", value=f"基本 {BASE_INCOME} + 税収 {tax} = **{BASE_INCOME + tax}**")
    if debt and debt > 0: embed.add_field(name="借金 (戦債残高)", value=f"**{debt}** Gold\n(給付から自動天引き)")
    await interaction.followup.send(content=get_promo_and_tip(), embed=embed, ephemeral=True)

async def run_diplomacy(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    view = discord.ui.View(timeout=60)
    view.add_item(DiplomacyActionSelect(world_id))
    await interaction.followup.send(f"**外務省パネル** [世界#{world_id}]\n実行したい外交アクションを選択してください。", view=view, ephemeral=True)

async def run_targets(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT iso_alpha, owner_id FROM territories WHERE guild_id=? AND world_id=?", (guild_id, world_id))
        occupied = {row[0]: row[1] for row in c.fetchall()}
        c.execute("SELECT iso_alpha FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        my_lands = [r[0] for r in c.fetchall()]
        
    adj_targets = set()
    for land in my_lands: adj_targets.update(ADJACENCY_GRAPH.get(land, set()))
    unoccupied_lands = [code for code in VALID_CODES if code not in occupied]
    enemy_lands = [code for code, owner in occupied.items() if owner != user_id and not is_allied(guild_id, world_id, user_id, owner)]
    
    def format_land(code): return f"`{code}`*" if code in adj_targets else f"`{code}`"
    
    embed = discord.Embed(title=f"侵攻可能な国家・地域一覧 [世界#{world_id}]", description="*マークは自国領土と隣接しています。", color=0xe74c3c)
    embed.add_field(name="無所属の空き地", value=" ".join([format_land(c) for c in sorted(unoccupied_lands)])[:1024] if unoccupied_lands else "なし", inline=False)
    embed.add_field(name="敵対国・他人の土地", value=" ".join([format_land(c) for c in sorted(enemy_lands)])[:1024] if enemy_lands else "なし", inline=False)
    await interaction.followup.send(content=get_promo_and_tip(), embed=embed, ephemeral=True)

async def run_country_management(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT iso_alpha, defense FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        territories = c.fetchall()
    if not territories: return await interaction.followup.send("[エラー] 管理できる領土を所有していません。", ephemeral=True)
    await interaction.followup.send(f"**領土管理 [世界#{world_id}]**\n下のメニューから管理したい領土を選択してください。", view=CountryManageMainView(territories, world_id, user_id), ephemeral=True)

async def run_country_status(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT iso_alpha, defense FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        lands = c.fetchall()
    if not lands: return await interaction.followup.send(f"[世界#{world_id}] 領土はありません。", ephemeral=True)
    embed = discord.Embed(title=f"自国領土の防衛状況 [世界#{world_id}]", description="\n".join([f"・ **{iso}**: 防衛力 {defen}" for iso, defen in lands]), color=0x2ecc71)
    await interaction.followup.send(embed=embed, ephemeral=True)

async def run_attack(interaction: discord.Interaction, target: str, world_id_override: int = None):
    await safe_defer(interaction, ephemeral=True)
    if world_id_override is not None:
        world_id = world_id_override
    else:
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return

    code = resolve_country_code(target)
    if code not in VALID_CODES: return await interaction.followup.send(f"[エラー] 存在しない国名です: {target}", ephemeral=True)
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT owner_id, defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, code))
        def_row = c.fetchone()
    
    defender_id = def_row[0] if def_row else None
    base_def = def_row[1] if def_row else DEFAULT_DEFENSE
    owner_name, warning_text = "無所属", ""
    base_cost_multiplier = 1.0

    if defender_id:
        if defender_id == user_id: return await interaction.followup.send("[エラー] すでに自国領土です。", ephemeral=True)
        try:
            owner_user = await interaction.client.fetch_user(int(defender_id))
            owner_name = owner_user.display_name
        except: pass
        if not is_at_war(guild_id, world_id, user_id, defender_id):
            base_cost_multiplier = 1.5
            warning_text = f"\n\n[警告] {code} は現在【**{owner_name}**】の領土です。\n宣戦布告なしで攻撃した場合、実質的な攻撃力が低下します。"

    distance_penalty = 1.0
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT adjacency_penalty FROM server_channels WHERE guild_id=?", (guild_id,))
        row_adj = c.fetchone()
        if row_adj and row_adj[0] == 1:
            c.execute("SELECT iso_alpha FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
            my_lands = [r[0] for r in c.fetchall()]
            if my_lands and not any(code in ADJACENCY_GRAPH.get(l, set()) for l in my_lands):
                distance_penalty = 1.5

    final_cost_multiplier = base_cost_multiplier * distance_penalty
    min_est = max(100, int(base_def * final_cost_multiplier * 0.8))
    max_est = max(100, int(base_def * final_cost_multiplier * 1.2))

    embed = discord.Embed(title=f"作戦司令部 [世界#{world_id}]", description=f"対象: **{code}**{warning_text}\n準備ができたら進軍を押してください。", color=0x3498db)
    embed.add_field(name="作戦推定コスト", value=f"推定必要資金: **{min_est} 〜 {max_est} Gold**\n*(※敵の防衛力や乱数により変動します。投入資金はそのまま全額消費されます)*")
    
    await interaction.followup.send(content=get_promo_and_tip(), embed=embed, view=AttackView(interaction.user.id, code, world_id), ephemeral=True)

async def run_un_list(interaction: discord.Interaction):
    if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM un_members WHERE guild_id=? AND world_id=?", (str(interaction.guild_id), world_id))
        members = c.fetchall()
    if not members: return await interaction.followup.send("加盟しているプレイヤーはいません。", ephemeral=True)
    embed = discord.Embed(title=f"国連(UN)加盟国一覧 [世界#{world_id}]", color=0x3498db)
    embed.description = "\n".join([f"・ <@{m[0]}>" for m in members])
    await interaction.followup.send(embed=embed, ephemeral=True)

async def run_camp_list(interaction: discord.Interaction):
    if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT camp_name, founder_id FROM camps WHERE guild_id=? AND world_id=?", (str(interaction.guild_id), world_id))
        camps = c.fetchall()
        if not camps: return await interaction.followup.send("現在、設立されている陣営はありません。", ephemeral=True)
        
        embed = discord.Embed(title=f"陣営一覧 [世界#{world_id}]", color=0x2ecc71)
        for camp_name, founder_id in camps:
            c.execute("SELECT user_id FROM camp_members WHERE guild_id=? AND world_id=? AND camp_name=?", (str(interaction.guild_id), world_id, camp_name))
            members = c.fetchall()
            member_count = len(members)
            embed.add_field(name=f"【{camp_name}】", value=f"設立者: <@{founder_id}>\n所属人数: {member_count}名", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

    # ==============================================================================
# アップデート情報読み込みロジック
# ==============================================================================
def get_versions():
    """updateフォルダ内のtxtファイルを取得し、新しい順にソートして返す"""
    update_dir = "update"
    if not os.path.exists(update_dir):
        return []
    
    versions = []
    for filename in os.listdir(update_dir):
        if filename.endswith(".txt"):
            versions.append(filename[:-4]) # .txtを消して追加
            
    # v1.0, v1.10 などのバージョンを正しく(1,0), (1,10)として比較して降順ソート
    def parse_version(v_str):
        clean_v = v_str.lower().replace('v', '')
        try:
            return tuple(map(int, clean_v.split('.')))
        except ValueError:
            return (0,)
            
    versions.sort(key=parse_version, reverse=True)
    return versions

async def run_version(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    versions = get_versions()
    if not versions:
        return await interaction.followup.send("[エラー] `update` フォルダ、またはバージョンファイルが見つかりません。", ephemeral=True)
        
    top_versions = versions[:10] # 最新から10件
    
    embed = discord.Embed(title="☁ 閲覧可能なバージョン一覧", color=0x3498db)
    description = ""
    for v in top_versions:
        description += f"・ **{v}**\n"
    
    description += "\n内容を見るには `/update [バージョン]` と入力してね！"
    embed.description = description
    
    await interaction.followup.send(embed=embed, ephemeral=True)

async def run_update(interaction: discord.Interaction, version: str = None):
    await safe_defer(interaction, ephemeral=True)
    versions = get_versions()
    if not versions:
        return await interaction.followup.send("[エラー] `update` フォルダ、またはバージョンファイルが見つかりません。", ephemeral=True)
        
    # 引数がなければ最新バージョンを使用
    target_version = version if version else versions[0]
    
    file_path = f"update/{target_version}.txt"
    
    # もし「1.0」と入力されたら「v1.0」として探す優しさフォールバック
    if not os.path.exists(file_path):
        if not target_version.startswith('v') and os.path.exists(f"update/v{target_version}.txt"):
            target_version = f"v{target_version}"
            file_path = f"update/{target_version}.txt"
        else:
            return await interaction.followup.send(f"[エラー] バージョン `{target_version}` の情報が見つかりません。", ephemeral=True)
            
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        embed = discord.Embed(title=f"☁ 全世界戦争Bot アップデート情報 ({target_version})", description=content, color=0x3498db)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"[エラー] ファイルの読み込みに失敗しました: {e}", ephemeral=True)

async def run_invest(interaction: discord.Interaction, amount: int):
    await safe_defer(interaction, ephemeral=True)
    if amount <= 0: return await interaction.followup.send("[エラー] 投資額は1 Gold以上にしてください。", ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT gold, invest FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        row = c.fetchone()
        if not row: return await interaction.followup.send("[エラー] 国家データが見つかりません。", ephemeral=True)
        current_gold, current_invest = row[0], (row[1] if row[1] else 0)
        
        if current_gold < amount: return await interaction.followup.send(f"[エラー] 資金不足です。(現在: **{current_gold} Gold**)", ephemeral=True)
        
        new_gold = current_gold - amount
        new_invest = current_invest + amount
        c.execute("UPDATE players SET gold=?, invest=? WHERE guild_id=? AND world_id=? AND user_id=?", (new_gold, new_invest, guild_id, world_id, user_id))
        conn.commit()
        
    await interaction.followup.send(f"📈 **[投資完了]**\n国家インフラに **{amount} Gold** を投資しました！\n現在の総投資額: **{new_invest}**" + get_promo_and_tip(), ephemeral=True)

async def run_research(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT gold, oil, tech_level FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        row = c.fetchone()
        if not row: return await interaction.followup.send("[エラー] 国家データが見つかりません。", ephemeral=True)
        
        gold, oil, tech_level = row[0], row[1], (row[2] if row[2] else 1)
        cost_gold = tech_level * 1000
        cost_oil = tech_level * 500
        
        if gold < cost_gold or oil < cost_oil:
            return await interaction.followup.send(f"🧪 **[研究失敗: 資源不足]**\n**Lv.{tech_level+1}** への研究には以下の資源が必要です。\n・資金: **{cost_gold} Gold** (現在: {gold})\n・石油: **{cost_oil} L** (現在: {oil})", ephemeral=True)
        
        c.execute("UPDATE players SET gold=gold-?, oil=oil-?, tech_level=tech_level+1 WHERE guild_id=? AND world_id=? AND user_id=?", (cost_gold, cost_oil, guild_id, world_id, user_id))
        conn.commit()
    await interaction.followup.send(f"🔬 **[研究完了]**\n技術研究が成功しました！国家のテックレベルが **Lv.{tech_level+1}** に上昇しました！" + get_promo_and_tip(), ephemeral=True)

async def run_spy(interaction: discord.Interaction, target: str):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id, target_code = str(interaction.guild_id), str(interaction.user.id), resolve_country_code(target)
    cost = 1500
    
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT gold, tech_level FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        row = c.fetchone()
        if not row or row[0] < cost: return await interaction.followup.send(f"[エラー] 資金 **{cost} Gold** が必要です。", ephemeral=True)

        c.execute("SELECT owner_id, defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, target_code))
        t_row = c.fetchone()
        if not t_row: return await interaction.followup.send(f"[エラー] {target_code} は中立国か、存在しない国です。", ephemeral=True)
        if t_row[0] == user_id: return await interaction.followup.send("[エラー] 自国にスパイは派遣できません。", ephemeral=True)

        tech_level = row[1] if row[1] else 1
        success_rate = min(95, 60 + (tech_level * 2))
        is_success = random.randint(1, 100) <= success_rate

        c.execute("UPDATE players SET gold=gold-? WHERE guild_id=? AND world_id=? AND user_id=?", (cost, guild_id, world_id, user_id))

        if is_success:
            c.execute("SELECT user_name, gold, oil FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, t_row[0]))
            enemy = c.fetchone()
            msg = f"🕵️ **[潜入成功]**\n**{target_code}** への潜入に成功しました！\n\n**【極秘データ】**\n・統治者: **{enemy[0]}**\n・現在防衛力: **{t_row[1]}** 人\n・国家資金: **{enemy[1]}** Gold\n・石油備蓄: **{enemy[2]}** L\n・潜入成功率: {success_rate}%"
        else:
            msg = f"💥 **[潜入失敗]**\n防衛網に引っかかりスパイは捕らえられました...\n工作資金 **{cost} Gold** を失いました。(成功率: {success_rate}%)"
        conn.commit()
    await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)

async def run_propaganda(interaction: discord.Interaction, target: str):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id, target_code = str(interaction.guild_id), str(interaction.user.id), resolve_country_code(target)
    cost = 3000
    
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT gold, tech_level FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        row = c.fetchone()
        if not row or row[0] < cost: return await interaction.followup.send(f"[エラー] 資金 **{cost} Gold** が必要です。", ephemeral=True)

        c.execute("SELECT owner_id, defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, target_code))
        t_row = c.fetchone()
        if not t_row: return await interaction.followup.send(f"[エラー] {target_code} は中立国か、存在しない国です。", ephemeral=True)
        if t_row[0] == user_id: return await interaction.followup.send("[エラー] 自国には工作を行えません。", ephemeral=True)

        tech_level = row[1] if row[1] else 1
        success_rate = min(90, 40 + (tech_level * 3))
        is_success = random.randint(1, 100) <= success_rate

        c.execute("UPDATE players SET gold=gold-? WHERE guild_id=? AND world_id=? AND user_id=?", (cost, guild_id, world_id, user_id))

        if is_success:
            drop_amount = max(10, int(t_row[1] * random.uniform(0.10, 0.20)))
            new_def = max(100, t_row[1] - drop_amount)
            c.execute("UPDATE territories SET defense=? WHERE guild_id=? AND world_id=? AND iso_alpha=?", (new_def, guild_id, world_id, target_code))
            msg = f"📢 **[工作成功]**\n民衆を扇動し暴動を起こさせました！\n混乱により現地の防衛力が **{t_row[1] - new_def}** 減少しました！ (現在の防衛力: {new_def})"
        else:
            msg = f"❌ **[工作失敗]**\nプロパガンダは見破られ強制送還されました...\n工作資金 **{cost} Gold** を無駄にしました。"
        conn.commit()
    await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)        
# ==============================================================================
# 不戦協定 (Peace Treaty)
# ==============================================================================
class PeaceTreatyView(discord.ui.View):
    def __init__(self, guild_id: str, world_id: int, proposer_id: str, proposer_name: str, target_id: str):
        super().__init__(timeout=86400)
        self.guild_id = guild_id
        self.world_id = world_id
        self.proposer_id = proposer_id
        self.proposer_name = proposer_name
        self.target_id = target_id

    @discord.ui.button(label="承認する", style=discord.ButtonStyle.success, emoji="🕊️")
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.target_id: return await interaction.response.send_message("あなたへの提案ではありません。", ephemeral=True)
        await interaction.response.defer()
        
        now = datetime.datetime.now(datetime.timezone.utc)
        expires = (now + datetime.timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO peace_treaties (guild_id, world_id, user_a, user_b, expires_at) VALUES (?, ?, ?, ?, ?)", (self.guild_id, self.world_id, self.proposer_id, self.target_id, expires))
            conn.commit()
            
        add_world_log(self.guild_id, self.world_id, f"{self.proposer_name} と {interaction.user.display_name} が24時間の不戦協定を締結しました。")
        self.stop()
        await interaction.message.edit(content=f"🕊️ **不戦協定締結**\n{self.proposer_name} と {interaction.user.display_name} は、向こう24時間の不戦協定を結びました！", view=None)

    @discord.ui.button(label="拒否する", style=discord.ButtonStyle.danger)
    async def btn_reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.target_id: return await interaction.response.send_message("あなたへの提案ではありません。", ephemeral=True)
        await interaction.response.defer()
        self.stop()
        await interaction.message.edit(content=f"💥 **協定拒否**\n{interaction.user.display_name} は {self.proposer_name} の不戦協定提案を蹴り飛ばしました。", view=None)
