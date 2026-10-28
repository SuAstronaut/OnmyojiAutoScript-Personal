"""Offline only: unittest + original PNGs + existing local OCR models.

Run with toolkit/python.exe -B tasks/ActivityShikigami/test_ap_guji.py.
No application Config/Device is constructed, no game input, no dependency install.
The logger is isolated before imports (the normal logger prunes log files).
"""
import ast
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


class QuietLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


sys.modules['module.logger'] = types.SimpleNamespace(logger=QuietLogger())
import cv2
import numpy as np
from module.ocr.rapid_ocr import RapidOcrModel
from module.atom.image import RuleImage
from tasks.ActivityShikigami import ap_guji as ap
from tasks.ActivityShikigami.assets import ActivityShikigamiAssets as Assets
from tasks.ActivityShikigami.config import ActivityShikigami
from tasks.ActivityShikigami.script_task import ScriptTask
from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.GameUi.page import page_main

STAMPS = ('170700_570620', '170710_379149', '170714_696721',
          '170731_712337', '171237_305329')
S = ap.ApState


# Frozen pre-AP AST fingerprints, captured from this immutable commit.
# Keep the baseline in this test: commits and source archives must not change it.
# The original comparison scope is preserved: 36 methods (excluding _run_ap)
# and all 100 legacy asset declarations. AP dispatch is stripped below as before.
LEGACY_BASELINE_COMMIT = '5617bc616e57988fd9218b04e080a5fa70959295'
LEGACY_METHOD_SHA256 = {
    '__init__': '8c75709df5be920324fc637d679c22b026383bce7984a7e53a4c8337a5d09f02',
    '_auto_cancel': 'd790901c148bccbb4f1b10b4582b4f6d96ce1b36bd967e9c58863443513b6b57',
    '_auto_seg_remaining': '01788707cde5fa18d5df546ebcf3351a8c508fceb6782198a1e074928e2b9d62',
    '_auto_start': 'a20bad8014152701693cf1f7db103b674bdcb239f1817934eceb642d90d42422',
    '_boss_early_exit_warn': '8a59339ac8b5cd449693faf24cc085ed9cd45371d0c5496ff422e7fd80da9c8f',
    '_run_ap20': '363e9a08ec8cb7223e78717a5c3c5a28f07f7ce9f30899f9c03a2ea085063a18',
    '_run_boss': '4afde874d265e864b0a32a8db9ba7dc709efdaae2f7fbc92a6e58d2e60e407d5',
    '_run_pass': 'ce2ab799a0dc6267c56166ec8e4ba6f4da7e20abe851ce249b6f7b02cf649fc4',
    '_start_battle_ap20': 'f403acabde6058888972a2b5c0ec32ecac9f0acaa35aa3fdeb8f5c39bfe3a2c2',
    'activity_shikigami_battle_wait': '797cd2f5a8bd6f2b0e98f20402d40ef19a2eef8076c2623217eb4dee79b82faa',
    'auto_battle_count_hook': 'a872b811ad068160cd36a99bfd2d9e8d6530489f80da5221a95b2c39c677b2dd',
    'auto_battle_remaining_now': 'eee9b042fdfe72fb645f1454b37497565a3516ac77cf98234e9660c682c4bdb3',
    'auto_battle_run': '816a8402970f4c9d034abc59a5e31dfa0d7862292b370e7bfa6b4b529e98b9cc',
    'battle_wait': 'e5937dd7a8e76daa9a87aacf78caa1ec31d57d3f31dbb9116f6bba6d7b805c59',
    'boss_early_exit_ready': '3e5e44d04bb20536529cc52e51ac949223be47904b502a48b488115680d2e132',
    'boss_early_exit_trigger': 'f22dcd96e9da968b37acec344d267f3b8487f47ad9646c568247b95c90a5afeb',
    'boss_fire_text_present': '967db60cf4bdcf9e207c59d3f28b9f4a323dc74595368d1f0949407c8547c9d3',
    'check_tickets_enough': 'ee4272a61658bacd35bc52f5632dfd426bfe704d15afc648c797a15d86af355d',
    'get_general_battle_conf': 'f67b35c43ca15eced4a1ff9096858f396913f35c2165fd2a711ee560c52bbd39',
    'is_auto_battle_page': '3ca2b15fcd285b52251b49cd03fa3c65efb1f7e67d60b5ab910a9c978824704a',
    'is_x5_on': 'a58c494ec979955c7655de621b4dfd99cb092dca79665934c23d9b7412f3dca5',
    'lock_team': 'a57a151e1d9bdca0ee9ebd88bc08e0542f684e62b176a29585a211fcab6a580c',
    'quit_boss_battle': '8c04afd7c4c56b06fc652e125360d6fc4ff117b716017f7dab6f9472908a6361',
    'random_reward_click': '8b85d342d429a96401ac0b974cff44e7c314b3d3d9c97cd189564096d2115690',
    'read_x5_ticket': 'c77193edff029ca40cc3edeb5097550ff204d7d7b0324ecf7101d736e32d878a',
    'reward_forbidden': 'e88b284106b0a9e895873fe3ee5eedaac92b4b5e7e31edeb55c22093a80b5810',
    'reward_grid_detect_enabled': '76f7f34ff764e3932b3a56ce8232643cf44983994f74ad68a099698d69f32757',
    'reward_hot': 'e621a2dcde6d642389752e23927ef863c74ab209bc1b39efde4ecf37b63ad24b',
    'run': '8d4e1171835808cefc07fe1f0a1d37c0615cd0fe72979c9e6622235fcf2eea3e',
    'run_general_battle': '3b367eeca65dc55aa538d4f3b1ee28b5f46877e751dbd587f2e483948f6e0323',
    'settlement_click_count': 'f039617baae4ff77fb656896af4d000ef4501bc165032e7338220e17b1172733',
    'start_battle': '91024f9464c99fb4d3a95ad57ce27cb4793e44e4e82423e679e73aacd2aaf843',
    'switch_climb_mode_in_game': '65bbeb304799d2cb05892b416fb8a011d514b8e9115fd87a783aea3311310b25',
    'switch_soul': 'f150787ddafd75e384c19f8a85ab28da262c8c8350b6c28d956a2ee82c2bc9e6',
    'switch_x5_in_game': '20f05336d00ce3b2f3c7082e8087236c4b2e4693868e8f51289ebff84e5644a2',
    'update_x5_state': '2787d74dcff9502650fd73d04000d4beb939aff8c06d5d4568e060b64756d3c6',
}

LEGACY_ASSET_SHA256 = {
    'C_MONOPOLY_DICE_1_CLICK': '38a379871da0a2e0946d50ef45995f73fd75cd7ddbab82e84b8147e05ab97a85',
    'C_MONOPOLY_DICE_2_CLICK': '85b400f7a58dc936dc35158c32c2a295350e4dd3e14d9525ace3f52a7988b1b0',
    'C_MONOPOLY_DICE_3_CLICK': 'eea5f4a0378fcf17ac2f96d8b06e5d8bcdc352fb78ca9072ef571fcf125f4882',
    'C_MONOPOLY_QA_ANSWER_1': 'a1f42b7fc9f3aba8b88c4027d5c8263418fe208ddaab9c81877f006e42e5bd79',
    'C_RANDOM_ALL': '1071ed1b2e860d7b3704f2c577ffc5101963c57fd2cc61fdec387020b977836d',
    'C_RANDOM_BOTTOM': '0e97831616ec73538329cc3d74332c7ed40e1e1f0db25172b42e558e2bdf7945',
    'C_RANDOM_LEFT': '3882ed333063d843419d24b9973e3ac583ed788a1fd7858aa31c4def4f8047a3',
    'C_RANDOM_RIGHT': '49b10c54af85a891525fa8dcd3ce318014d917fcc79240925ac7d9dd2668e4ad',
    'C_RANDOM_TOP': '0083dc63c0732c85367a24edd2b7df4a8d433a6c96ac3df1857f2c22f86ab807',
    'C_SEASON_BOSS_CARD': 'a3659717b7fb37cb02b9815711ca746590e10210f435482be3b8b90ee2e2e70c',
    'I_A_REWARD': '29419f92861af4edbb2ce1ee0caf0cef972c8bcd5e13683cd62ca41e06a53a53',
    'I_AP20_ENTRY_0': '3e4417860b772bb3f3a2d8cd481b60174016bec6b9e3161b6590dbc884ea71e1',
    'I_AP20_ENTRY_1': '23de715c4d76947322fa40dbb596afa937e23c738e166113501b641dc490dca5',
    'I_AP20_ENTRY_2': '5c4bf97207fec32c4ca367b0b05657b7052e7ec27c71542f243ffb16394dae83',
    'I_AP20_ENTRY_3': 'c4232dc0db85e30a70c71ad2df9f2e2e7afc83c054576b091076c5f2ac674942',
    'I_AP20_ENTRY_4': 'c10f3ae454102bd574c31c9506d2755d6d4731cec41adcebe65569d5f8b09061',
    'I_AP20_ENTRY_5': 'bbfe8673f4f9f8351a3cdbfd1e10bb70b3e647a060b28d9ccc1f6bb59220e5e0',
    'I_AP20_ENTRY_6': '084775a1d963ee2cda42bfc306db28d92be083bf11cd9729a0fb11ecf0a3d0bd',
    'I_AS_REWARD_GOLD': '4cae975322aacfb95c54fb48fcca91bcb17970194c29220550f49639823ce959',
    'I_BACK_GREEN': 'f85330e820414d6e6d48b2a5eeaa9427c704f312fe433395f9589e5b88fb7428',
    'I_BATTLE_MAIN_TO_RECORDS': '01006f936be71ccb4dca0cf994d35c8a971f776ffdd663d60f2f6fae4d3b6533',
    'I_CHECK_AP20': '3c5d3cb472d615d9c51eb2ef5e1fda4b33a30e4cb20aa35e425a9a60b0e19ead',
    'I_CHECK_AP20_BOSS': '721b2c2953d55f03f88223ca2e8f8ac95e5d53bf73c43bb9e1254035fbf2ce1f',
    'I_CHECK_BATTLE_BOSS': '6318b2c647f350c4ea8247fb94a2110693c128d192cd7d282fdaa6bceba9507b',
    'I_CHECK_BATTLE_MAIN': 'd953b1a151f7d9cb59633aa381fb3e7c87d765530014bb00cf8f25c72702a17b',
    'I_CHECK_MONOPOLY_MAIN': '0455ab9ed4cf3b7d5e779e18f4d67452ccc35d70e0d2262a4312177847628220',
    'I_CHECK_SEASON_BOSS_MONSTER': '9cacfcb17666fca7e9167a0463799dcac3c145f03c59684f4861ef181276d065',
    'I_CLIMB_MODE_AP': '4435e40d98cd48a04ba5ea55ddca0bd6eedc5ead1e52efeb5e5c262db2401553',
    'I_CLIMB_MODE_PASS': 'c7a8f1b4f784854e2579249be3124e93be4492defe59e20180c3424e228ab1c5',
    'I_CLIMB_MODE_SWITCH': 'ebce339cbd52e8d1ef4fae72bf2cd96169eb62490d4b7a665fd122ab883f4b81',
    'I_LOCK': '39768dfcf28e0eb1d9ed48116d63451b3ee380c3dcd0654ae8fae0a41a2d4c82',
    'I_MONOPOLY_BUY_BTN': 'ca049cb7d2417a5075532b05b24c55abbfc235ccd21fa889643b125694174f26',
    'I_MONOPOLY_BUY_ENABLE': '103a68ac08002a133ba09fa5063ca57f71762b3b13200ad75d2de4bb9a358270',
    'I_MONOPOLY_DICE_1_ICON': '0ddf2e664df513555a4e3472bca7eae2f65fe3351334be1251a2ded5be692035',
    'I_MONOPOLY_DICE_2_ICON': '200b56bd0709cc5beb0db578e30b234cd1efd51587b8cd055e54c3ee545e35e4',
    'I_MONOPOLY_DICE_3_ICON': '15fcac884370e6960fc775fbebde2f19233fc3fa3e32b95004ad8f9410bddc0a',
    'I_MONOPOLY_FIRE_BTN_DICE': '291b735b1ca9465d0ce6e9cca04560c954cc13c22bfe753722e52f01c48db83b',
    'I_MONOPOLY_FIRE_BTN_NORMAL': '4eba429e7edaeb7b11102d999ef8584c196433d66008e42aad6386309329d209',
    'I_MONOPOLY_FIRE_SWITCH': '88d8b06e512117299a8b1445f8881a04b2cb6ad369265770defbe97ec6fd4273',
    'I_MONOPOLY_PAGE_FIRE': '6eac21f3f7d3f5db05b927d909ea638b35883bd2dc20da14b8a643dec410c0fa',
    'I_MONOPOLY_PAGE_FIRE_DICE': '7bae2e42421031a846b8bbdf6b13d240959facd381b7e677afee66dca2f71db2',
    'I_MONOPOLY_PAGE_QA': 'd5993fcb49fd68185cadb6d5ae1031c33751d21c79e1e500b0a8da2216f50023',
    'I_MONOPOLY_PAGE_ROLL': 'd7b5ca894ccc80942ae279728f04146fcc8d0638ac5cf477ca47552ca46b23e2',
    'I_MONOPOLY_PAGE_SELECT_DICE': '025a5e4ae7437874a5caa45e78dae2a5a3f4f57a55e09c1fdf2e742075509205',
    'I_MONOPOLY_PAGE_SHOP': 'f8c465f716209785504c9e1da90f81394ee5abdc791b47d7b6d8c38e2000abd0',
    'I_OFF_X5': 'e0411fc5b264fc78b1d0c93e78a3df67c25dd3b1032b16dc7c66235a3a953328',
    'I_ON_X5': '9513d07cbb3426e67adcaccec6452a8ffb9ace1eaaac848d0c8741977e1760d0',
    'I_PASS_1': '1dad2ff6c78178b1a28897335eb85813b27dfefda5088c41e5607f60934afc5e',
    'I_PASS_10': '4c547513c747aa280816bca74526c11589b9c9c465424b5665c9211a4046be8a',
    'I_PASS_11': 'af64cf44ab862d53353d6badb49b3b56b4f9a589ab971220f0a688ce60dee83e',
    'I_PASS_13': '09454fb4c915b30626b90cfa9a619aa4f5fabc206a841b0ac3c8c2eae5d602e9',
    'I_PASS_2': '544cdb6d744de7d8afc2275009b718221d7c72cfbf4dc70c969b0eea79ed6704',
    'I_PASS_3': 'bb5d2e8314775f16de21a113b3ae0c427b00f39edb0f9feaa3cbf6f08a28ef2b',
    'I_PASS_4': '21e2f288b78da96d8e68cd5ddb632385c97ef898af00cc0d8f0f24ed60ef031d',
    'I_PASS_5': '84c3eae3022c6463b002dc8c1b3bcb265e02ba73b4f487751d105e7d525d0a1d',
    'I_PASS_6': '5b518db7185198690163a4c88218da25d681b71e5f0d874ea42cc156a68d212c',
    'I_PASS_7': 'c6920cd9f49e34b1eeea29368ebb3d67cb0445d4daa54f7b299f8d31faccb81a',
    'I_PASS_8': '063a46db4c7354807697717bd427f19714f032de351b53a7fdab62ddb9c7ad15',
    'I_PASS_9': '3be8da00764975888f27b4dc1a188abea60d572a305c06ae1ef0da7af3168e9a',
    'I_PASS12': '9fdf100d7c36048ac43c0440686a9205e3a99ed2e4fcaefd9f3d6f7ed66ec35b',
    'I_PURPLE_SNAKE_SKIN': 'f45edca123933e2f8d4ce1e09d279188302aae5ed31943bd0b57d5e07e2ac9af',
    'I_RED_EXIT': 'f62cc8f605f840287fe213a2d6e381754662303e7d123d9823cec0f65ddd06c7',
    'I_SEASON_BOSS_CARD': 'f982d5646f9763aee440de112db92921017676b901534c5aca0c13da9b0d8f21',
    'I_SEASON_BOSS_DISCOVER': '3a2363b8e011cf5e14dfdda9ba63fa6cae0832f102b98dab44fc50027ad24f64',
    'I_SEASON_BOSS_DISCOVER_PREMIUM': 'b018118a99dd965f7d7955d14b7644484ee309ec178d2909680c64cccf033269',
    'I_SEASON_BOSS_FIGHT_FREE': 'd6b22bdbd0aeceaabbb863d63f1af6b41487312064edb0ba9c5ca3ee57e02f18',
    'I_SEASON_BOSS_FIGHT_PAY': 'a4371cd8fa9ee2aa33df7074368cc7ad19899b2b122335f13baa94b070135d12',
    'I_SEASON_BOSS_LOCKED': 'da2d8ddb12731732a6c9fa3c1474f63c73aa278963baf14a3523522822494dd0',
    'I_SEASON_BOSS_MODE_SWITCH': '68490de08b48b129acbbebd1174c27e4561f3fcf34382532a875c3255f57336a',
    'I_SEASON_BOSS_RED_EXIT': 'e147e68d87283c43eb2f6433a03fb7ffc509e0143dcd73310cd6fa4a1bd3624c',
    'I_SEASON_BOSS_SHIKIGAMI_RECORD': '489e37ec23ef34a7255fd73b64fe6f792fd8b674195150281db1eb4e38513ebf',
    'I_SHI': 'dbc9d361c35d428aa12da33d223ed3873adf5275f1d381c06bcf1235b2b8d382',
    'I_SKIP_BUTTON': '4a50c3a7df0394e370f2e3ca5f4b6921d28d27256a9e15929fb72bccfaed48d8',
    'I_TO_AP20': '5419d77df5662ade62a70560dd0e8728f744210ba5020b424879642fddfb146b',
    'I_TO_AP20_BOSS': 'e548515e9046aa64e387c31846a575cef08babbde5f2f2ae70cbdb258e9190c4',
    'I_TO_BATTLE_BOSS': '8ba34613a85498604025a6b42f258211b28ead1483150ac895bb4c4ce9b6dfb4',
    'I_TO_BATTLE_MAIN': '18ba1c9b49b530a50cdef3dce69c3a16843911be9f86a79818af82ee6ce3ef29',
    'I_TO_BATTLE_MAIN_2': 'c029df8bcd75c06c3258c950c514f81841d117cb2e837f24b2de144994103d75',
    'I_TO_MONOPOLY_MAIN': '4314ec5cf3b466c5dcf9038316e7e68d635cec515535a6ae165577c65a528274',
    'I_UNLOCK': '3a7ca034735e09d6ce8d77b7895667873d2bad9690be81bbe4898c42406d83a7',
    'O_BOSS_SCORE': 'b80605227aefa06ff9a41c1088bc45297a9e1e1470cadf99f7710107cc9f7f0f',
    'O_ENTER_AP100': 'dfb8fa86a142db2c6e57ff7d93173dbb3849bc29583a35546a4d31e89548e255',
    'O_FIRE': '4780aa8d7c783b0b9d45ec8e3fb1ad581146747960b012c5c9d2d812b10a0c51',
    'O_FIRE_AP20': '3d2eeaab11e42c37b40f6796991c5b59b7ed79106d4d0cfef9ccb40ff542fc4a',
    'O_FIRE2': '40faeb3c6c61e98542db05ad3a9398e548a7cfca745d38381d0d605058915f00',
    'O_MONOPOLY_REMAIN_PASS': '4d7e4e0c1c811ce681a4db74d964d2ec019ac3ed5cf14edbc2002f2c0748802b',
    'O_REMAIN_AP': 'f69226eaee350d14de4ff224b3347ffa29097e1932f14214e2b7f9a3542559ae',
    'O_REMAIN_AP100': 'd9bec5f9257d5aa45043bf5a165e58c59f34e1def272c7fc973b180bf46bd633',
    'O_REMAIN_AP20': '3153d0f3e9fdf8867c5613d357d857414494d609f5a22f07879cd4babae650ec',
    'O_REMAIN_BOSS': '61cdc772a870b0dbcaad65cff8fe5b0cbf70ac5c84ca2761d4c09a919a501bcd',
    'O_REMAIN_BUY': '461d37adbe32c2a4a44b7a7122e0040c993e30153fb8944493299f473ac1a613',
    'O_REMAIN_PASS': '26439f99af7e619c1deed30586d2ca629a918b72fd451bdbb8e0d18bff9a1063',
    'O_REMAIN_PASS2': '87acbbb0fc4d8666999e9f58c9ad790a5b7516aa45544c3458f9603f00ff2c98',
    'O_REMAIN_X5': 'bd03be9253e8c3a1d345a38d24e30991913b42f8144a52c77c23b7b5a879fb6d',
    'O_SEASON_BOSS_CHECK_MAIN': 'd8087f184d190ab14f68449e867ef3197bb3b6e84403b17fd2f735071faef26a',
    'O_SEASON_BOSS_MONSTER_NAME': '65a24ce16643ceda5a232bc9f91dc22bb8c2a2ebfbd076e13d0ad059710c6293',
    'O_SEASON_BOSS_RANK': 'acc7d62d21d04456eb9f4e2a2af4a735ec6f88606726ed36f60d38631ed7bdee',
    'O_SEASON_BOSS_REMAIN_NORMAL': 'f68fc7f901288413b5a8f6b3fc856f87bb6b5bb7475a7f4767587617dfcc1d26',
    'O_SEASON_BOSS_REMAIN_PREMIUM': 'af05c5665141173cd6ad495f0a60558e8a8b6caff86509993e6b776fc8b71d43',
    'S_AP20_SWIPE_LEFT': '3f1b063e92bfdcf4c7053a1f15628b2e4f14c12b32cc6f428e1c0af97ca87eae',
}


def legacy_ast_sha256(node):
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode('utf-8')).hexdigest()


def read_png(path):
    return cv2.cvtColor(cv2.imdecode(np.fromfile(path, dtype=np.uint8), 1), cv2.COLOR_BGR2RGB)


class Clock:
    def __init__(self):
        self.now = 0.

    def sleep(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now


class FakeDevice:
    def __init__(self, owner):
        self.owner = owner
        self.image = owner.images[2].copy()
        self.clicks = []
        self.on_click = lambda name: None
        self.on_frame = lambda: None

    def screenshot(self):
        self.owner.clock.sleep(.1)
        self.on_frame()
        mapping = {S.COURTYARD: 0, S.HOME: 1, S.CHALLENGE: 2, S.VICTORY: 3, S.REWARD: 4}
        if self.owner.forced_state in mapping:
            self.image = self.owner.images[mapping[self.owner.forced_state]].copy()
        return self.image

    def click(self, x, y, **kwargs):
        self.clicks.append((x, y, kwargs['control_name'], self.owner.clock.now,
                            self.owner.ap_state()))
        self.on_click(kwargs['control_name'])

    def stuck_record_add(self, *args):
        pass

    def click_record_clear(self):
        pass


class Harness(ScriptTask):
    def __init__(self, images, clock):
        # Deliberately bypass the production constructor: no Device/Config.
        self.images, self.clock = images, clock
        self.forced_state = None
        self.conf = ActivityShikigami()
        self.conf.general_climb.run_sequence = 'ap'
        self.conf.general_climb.ap_limit = 2
        self.config = types.SimpleNamespace(refresh_hot_at_checkpoint=lambda task: None)
        self.device = FakeDevice(self)
        self.interval_timer = {}
        self.current_count, self.run_idx = 0, 0
        self.start_time = datetime.now()
        self._ap_guji_active = True
        self._ticket_cache = {}
        self.scheduled = []

    def ap_state(self):
        return self.forced_state if self.forced_state is not None else super().ap_state()

    def ui_page_appear(self, page, *args, **kwargs):
        return self._match_page(page)

    def ap_snapshot(self, reason, image=None):
        self.saved_reason = reason
        if not hasattr(self,'snapshots'):
            self.snapshots = []
        self.snapshots.append((reason,(image if image is not None else self.device.image).copy()))

    def set_next_run(self, **kwargs):
        self.scheduled.append(kwargs)


class BaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.images = [read_png(ROOT / f'screenshots/desktop_capture/desktop_client_20261010_{s}.png')
                      for s in STAMPS]
        cls.insufficient_image = read_png(ROOT / 'screenshots/desktop_capture/desktop_client_20261010_184954_677576.png')

    def setUp(self):
        self.clock = Clock()
        self.task = Harness(self.images, self.clock)
        self.patchers = [patch.object(ap, 'sleep', self.clock.sleep),
                         patch.object(ap, 'monotonic', self.clock.monotonic)]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)


class ScreenshotTests(BaseCase):
    def test_all_templates_cross_reject_other_pages(self):
        positives = {'ENTRY': {0}, 'HOME': {1}, 'ROUTE': {1}, 'CHALLENGE': {2},
                     'FIRE': {2}, 'ACTIVITY_TOP': {2}, 'ACTIVITY_COST': {2},
                     'AP_COST': {2}, 'AUTO_IDLE': {2}, 'VICTORY': {3}, 'REWARD': {4},
                     'CONTINUE': {3}, 'CONTINUE_REWARD': {4}, 'BACK': {1,2},
                     'AP_TOP': {2}, 'REFRESH': {0}, 'X5_OFF': {2}, 'X5_ON': set(),
                     'LOCK': {2}, 'UNLOCK': set()}
        positives['INSUFFICIENT'] = set()
        for name, expected in positives.items():
            for index, image in enumerate(self.images):
                self.task.device.image = image
                with self.subTest(template=name, image=index+1):
                    self.assertEqual(self.task.ap_match(getattr(self.task,'I_AP_GUJI_'+name)), index in expected)

    def test_five_distinct_states(self):
        for image, state in zip(self.images, (S.COURTYARD, S.HOME, S.CHALLENGE, S.VICTORY, S.REWARD)):
            with self.subTest(state=state):
                self.task.device.image = image
                self.assertEqual(self.task.ap_state(), state)

    def test_entry_and_refresh_dynamic_locations(self):
        t = self.task
        t.device.image = self.images[0]
        self.assertTrue(t.ap_match(t.I_AP_GUJI_ENTRY))
        self.assertEqual(t.I_AP_GUJI_ENTRY.roi_front, [1188, 222, 38, 39])
        self.assertTrue(t.ap_match(t.I_AP_GUJI_REFRESH))
        self.assertEqual(t.I_AP_GUJI_REFRESH.roi_front[:2], [1195, 530])
        # Shift both icon and refresh substantially within their wide search ROIs.
        shifted = np.zeros_like(self.images[0])
        shifted[390:429, 1188:1226] = t.I_AP_GUJI_ENTRY.image
        shifted[310:333, 1195:1216] = t.I_AP_GUJI_REFRESH.image
        t.device.image = shifted
        self.assertTrue(t.ap_match(t.I_AP_GUJI_ENTRY))
        self.assertEqual(t.I_AP_GUJI_ENTRY.roi_front[:2], [1188, 390])
        self.assertTrue(t.ap_match(t.I_AP_GUJI_REFRESH))
        self.assertEqual(t.I_AP_GUJI_REFRESH.roi_front[:2], [1195, 310])

    def test_text_click_inside_six_character_box(self):
        t = self.task
        t.device.image = self.images[1]
        for _ in range(100):
            t.ap_click_match(t.I_AP_GUJI_ROUTE, 0)
        self.assertTrue(all(222 <= x <= 318 and 256 <= y <= 304
                            for x, y, *_ in t.device.clicks))

    def test_challenge_body_click_excludes_cost_controls(self):
        t = self.task
        t.device.image = self.images[2]
        t.ap_click_match(t.I_AP_GUJI_FIRE, 5)
        x, y, *_ = t.device.clicks[0]
        self.assertTrue(1134 <= x <= 1211 and 602 <= y <= 640)

    def test_legacy_x5_off_and_lock_positive(self):
        self.assertEqual(self.task.ap_x5_state(), ap.X5State.OFF)
        self.task.ap_ensure_single()
        self.task.ap_ensure_lock()
        self.assertEqual(self.task.device.clicks, [])

    def test_dark_overlay_is_not_challenge(self):
        self.task.device.image = (self.images[2] * .45).astype('uint8')
        self.assertNotEqual(self.task.ap_state(), S.CHALLENGE)

    def test_purchase_confirmation_never_clicked(self):
        t = self.task
        image = self.images[2].copy()
        rule = t.I_UI_CONFIRM
        mat = rule.image
        x, y = rule.roi_back[:2]
        image[y:y+mat.shape[0], x:x+mat.shape[1]] = mat
        t.device.image = image
        self.assertEqual(t.ap_state(), S.DIALOG)
        with self.assertRaises(ap.ApSafetyStop):
            t.ap_start_battle()
        self.assertEqual(t.device.clicks, [])

    def test_victory_and_reward_are_exclusive(self):
        for image, win, reward in ((self.images[3], True, False), (self.images[4], False, True)):
            self.task.device.image = image
            self.assertEqual(self.task.ap_match(self.task.I_AP_GUJI_VICTORY), win)
            self.assertEqual(self.task.ap_match(self.task.I_AP_GUJI_REWARD), reward)


class ResourceTests(BaseCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.model = RapidOcrModel(model_dir=str(ROOT/'toolkit/ocr_models'), device='cpu', cpu_threads=2)

    def test_actual_insufficient_template_text_and_zero_roi(self):
        t = self.task
        t.device.image = self.insufficient_image.copy()
        with patch.dict(t.O_AP_GUJI_INSUFFICIENT.__dict__, model=self.model), \
             patch.dict(t.O_AP_GUJI_ACTIVITY.__dict__, model=self.model):
            self.assertEqual(t.O_AP_GUJI_ACTIVITY.ocr(t.device.image), '0')
            self.assertTrue(t.ap_insufficient())
        self.assertTrue(t._ap_resource_exhausted)
        self.assertFalse(t.ap_start_battle())
        self.assertFalse(t.device.clicks)

    def test_actual_entry_timeout_frame_has_valid_challenge_and_284(self):
        t = self.task
        t.device.image = read_png(ROOT / 'screenshots/ActivityShikigami_Screenshots/ap_guji/20261010_194841_011072_Battle_entry_timed_out__no_second_challenge_click.png')
        self.assertEqual(t.ap_state(),S.CHALLENGE)
        self.assertEqual(t.ap_x5_state(),ap.X5State.OFF)
        self.assertFalse(t.ap_match(t.I_AP_GUJI_INSUFFICIENT))
        self.assertFalse(t.is_in_real_battle(False))
        self.assertFalse(t.is_in_prepare(False))
        with patch.dict(t.O_AP_GUJI_ACTIVITY.__dict__,model=self.model):
            self.assertEqual(t.O_AP_GUJI_ACTIVITY.ocr(t.device.image),'284')
        t.ap_click_fire()
        x,y,*_ = t.device.clicks[-1]
        self.assertTrue(1153<=x<1193 and 609<=y<633)

    def test_moved_toast_uses_matched_same_frame_roi(self):
        t = self.task
        t.device.image = self.images[2].copy()
        crop = self.insufficient_image[225:256,553:729]
        t.device.image[255:286,573:749] = crop
        old_roi = t.O_AP_GUJI_INSUFFICIENT.roi.copy()
        with patch.dict(t.O_AP_GUJI_INSUFFICIENT.__dict__, model=self.model):
            self.assertTrue(t.ap_insufficient())
        self.assertEqual(t.O_AP_GUJI_INSUFFICIENT.roi, old_roi)

    def test_similar_candidate_text_does_not_become_normal_exhaustion(self):
        t = self.task
        t.device.image = self.insufficient_image.copy()
        for text in ('', None, '琥珀卷轴', '琥珀卷轴充足', '体力不足'):
            with self.subTest(text=text), patch.object(t.O_AP_GUJI_INSUFFICIENT,'ocr',return_value=text):
                with self.assertRaises(ap.ApSafetyStop):
                    t.ap_insufficient()
        self.assertFalse(getattr(t,'_ap_resource_exhausted',False))
        self.assertFalse(t.device.clicks)

    def test_toast_candidate_ocr_exception_stops_without_input(self):
        self.task.device.image = self.insufficient_image.copy()
        with patch.object(self.task.O_AP_GUJI_INSUFFICIENT,'ocr',side_effect=ValueError('bad')):
            with self.assertRaises(ap.ApSafetyStop):
                self.task.ap_insufficient()
        self.assertFalse(self.task.device.clicks)

    def test_actual_ocr_and_registered_rules(self):
        for name, expected in (('AP', '3.4万'), ('ACTIVITY', '29'), ('X5', '0'),
                               ('AP_COST', '×6'), ('ACTIVITY_COST', '×1')):
            rule = getattr(self.task, 'O_AP_GUJI_'+name)
            with patch.dict(rule.__dict__, model=self.model):
                self.assertEqual(rule.ocr(self.images[2]), expected)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in ('AP','ACTIVITY','X5','AP_COST','ACTIVITY_COST'):
                stack.enter_context(patch.dict(getattr(self.task,'O_AP_GUJI_'+name).__dict__, model=self.model))
            self.assertTrue(self.task.ap_check_resources())
            self.assertEqual(self.task._ticket_cache, {})
            self.assertEqual(self.task._ap_resources, ap.ApResources(33000,29,0,6,1))

    def test_raw_quantity_and_conservative_abbreviation(self):
        self.assertEqual(ap.parse_quantity('12'), 12)
        self.assertEqual(ap.parse_quantity('3.4万', abbreviated=True), 33000)
        self.assertEqual(ap.parse_quantity('3.45万', abbreviated=True), 34400)
        self.assertEqual(ap.parse_quantity('3万', abbreviated=True), 20000)

    def test_invalid_ocr_formats_fail_closed(self):
        for text in ('', None, '12/50', '3.4', '3.4万体力', '-1', 'O', '1,000', '99999999999'):
            with self.subTest(text=text), self.assertRaises(ap.ApSafetyStop):
                ap.parse_quantity(text, abbreviated=True)

    def test_unknown_resource_units_or_costs(self):
        for cost in ('6', '×0', '', '×30'):
            with self.subTest(cost=cost), self.assertRaises(ap.ApSafetyStop):
                ap.resources_from_text('100', '29', '0', cost, '×1')
        self.task.device.image[664:689, 1090:1115] = 0
        with self.assertRaises(ap.ApSafetyStop):
            self.task.ap_read_resources()

    def test_sufficient_and_insufficient_each_resource(self):
        for av, ev, expected in ((6,1,True), (5,1,False), (6,0,False), (0,0,False)):
            self.assertEqual(ap.resources_from_text(str(av),str(ev),'0','×6','×1').enough, expected)

    def test_ocr_exception_is_safety_stop(self):
        with patch.object(self.task.O_AP_GUJI_AP, 'ocr', side_effect=ValueError('bad OCR')):
            with self.assertRaises(ap.ApSafetyStop):
                self.task.ap_read_resources()
        self.assertFalse(self.task.device.clicks)

    def test_unstable_two_frame_ocr_stops(self):
        with patch.object(self.task, 'ap_read_resources', side_effect=(
                ap.ApResources(100,2,0,6,1), ap.ApResources(100,1,0,6,1))):
            with self.assertRaises(ap.ApSafetyStop):
                self.task.ap_check_resources()

    def test_cached_session_reuses_costs_without_balance_ocr(self):
        t = self.task
        with patch.object(t, 'ap_read_resources', return_value=ap.ApResources(33000,29,0,6,1)) as read:
            self.assertTrue(t.ap_check_resources())
            for _ in range(8):
                t.screenshot()
                self.assertTrue(t.ap_check_resources())
            self.assertEqual(read.call_count, 2)
            # No invented decrement of the startup balance.
            self.assertEqual(t._ap_resources.activity,29)

    def test_cost_text_change_forces_revalidation_and_ocr_failure_stops(self):
        t = self.task
        with patch.object(t, 'ap_read_resources', return_value=ap.ApResources(33000,29,0,6,1)):
            self.assertTrue(t.ap_check_resources())
        t.device.image[664:690,1120:1160] = 0
        self.assertFalse(t.ap_session_matches())
        with patch.object(t, 'ap_read_resources',side_effect=ap.ApSafetyStop('bad OCR')):
            with self.assertRaises(ap.ApSafetyStop):
                t.ap_start_battle()
        self.assertFalse(t.device.clicks)
        self.assertIsNone(t._ap_session_key)

    def test_cached_session_never_tolerates_unknown_x5(self):
        t = self.task
        with patch.object(t, 'ap_read_resources', return_value=ap.ApResources(33000,29,0,6,1)):
            self.assertTrue(t.ap_check_resources())
        with patch.object(t,'ap_x5_state',return_value=ap.X5State.UNKNOWN):
            with self.assertRaises(ap.ApSafetyStop):
                t.ap_start_battle()
        self.assertFalse(t.device.clicks)

    def test_hot_five_times_config_invalidates_session(self):
        t = self.task
        with patch.object(t, 'ap_read_resources', return_value=ap.ApResources(33000,29,0,6,1)):
            self.assertTrue(t.ap_check_resources())
        t.conf.general_climb.five_times_enable = True
        with self.assertRaises(ap.ApSafetyStop):
            t.ap_start_battle()
        self.assertFalse(t.device.clicks)

    def test_page_reentry_invalidates_static_session(self):
        t = self.task
        with patch.object(t, 'ap_read_resources', return_value=ap.ApResources(33000,29,0,6,1)) as read:
            self.assertTrue(t.ap_check_resources())
            self.assertEqual(t.ap_navigate(),S.CHALLENGE)
            self.assertTrue(t.ap_check_resources())
            self.assertEqual(read.call_count,4)

    def test_x5_all_three_states_and_conflict(self):
        for on, off, expected in ((True,False,ap.X5State.ON), (False,True,ap.X5State.OFF),
                                   (False,False,ap.X5State.UNKNOWN), (True,True,ap.X5State.UNKNOWN)):
            self.assertEqual(ap.x5_from_matches(on,off), expected)

    def test_x5_on_is_physically_disabled_then_verified(self):
        t = self.task
        with patch.object(t, 'ap_x5_state', side_effect=(ap.X5State.ON, ap.X5State.OFF)), \
             patch.object(t, 'ap_click_match') as click:
            t.ap_ensure_single()
            click.assert_called_once_with(t.I_AP_GUJI_X5_ON)
            self.assertFalse(t._x5_active)

    def test_x5_unknown_stops_without_click(self):
        with patch.object(self.task, 'ap_x5_state', return_value=ap.X5State.UNKNOWN):
            with self.assertRaises(ap.ApSafetyStop):
                self.task.ap_ensure_single()
        self.assertFalse(self.task.device.clicks)

    def test_x5_does_not_turn_off_stops_after_one_toggle(self):
        with patch.object(self.task, 'ap_x5_state', return_value=ap.X5State.ON), \
             patch.object(self.task, 'ap_click_match') as click:
            with self.assertRaises(ap.ApSafetyStop):
                self.task.ap_ensure_single()
            self.assertEqual(click.call_count, 1)

    def test_x5_enabled_config_rejected(self):
        self.task.conf.general_climb.five_times_enable = True
        with self.assertRaises(ap.ApSafetyStop):
            self.task.ap_ensure_single()
        self.assertFalse(self.task.device.clicks)


class StateMachineTests(BaseCase):
    def configure_complete_flow(self, *, expire_in_battle=False, activity=29):
        t = self.task
        t.forced_state = S.COURTYARD
        t.balance = activity
        t.battle_started = None
        def click(name):
            if name == 'AP_GUJI_ENTRY':
                t.forced_state = S.HOME
            elif name == 'AP_GUJI_ROUTE':
                t.forced_state = S.CHALLENGE
            elif name == 'AP_GUJI_FIRE':
                if t.balance <= 0:
                    t.rejected_entry = True
                    return  # Game refuses entry and presents a short toast.
                t.forced_state = S.BATTLE
                t.balance -= 1
                t.battle_started = self.clock.now
                if expire_in_battle:
                    t.start_time -= timedelta(hours=1)
            elif name == 'AP_GUJI_SETTLEMENT':
                t.forced_state = S.REWARD if t.forced_state == S.VICTORY else S.CHALLENGE
            elif name == 'AP_GUJI_BACK':
                t.forced_state = S.HOME if t.forced_state == S.CHALLENGE else S.COURTYARD
        def frame():
            if t.forced_state == S.BATTLE and self.clock.now-t.battle_started >= 2:
                t.forced_state = S.VICTORY
        t.device.on_click, t.device.on_frame = click, frame
        actual_warning = t.ap_insufficient
        def warning():
            if getattr(t, 'rejected_entry', False):
                t.device.image = self.insufficient_image.copy()
                with patch.object(t.O_AP_GUJI_INSUFFICIENT, 'ocr', return_value='琥珀卷轴不足'):
                    return actual_warning()
            return actual_warning()
        return [patch.object(t, 'ap_insufficient', side_effect=warning),
                patch.object(t, 'ap_read_resources', side_effect=lambda: ap.ApResources(33000,t.balance,0,6,1)),
                patch.object(t, 'is_in_real_battle', side_effect=lambda *a: t.forced_state == S.BATTLE),
                patch.object(t, 'is_in_prepare', side_effect=lambda *a: False),
                patch.object(t, 'is_in_battle', side_effect=lambda *a: t.forced_state == S.BATTLE)]

    def test_full_production_run_two_battles_then_count_stop(self):
        from contextlib import ExitStack
        from module.exception import TaskEnd
        with ExitStack() as stack:
            for p in self.configure_complete_flow():
                stack.enter_context(p)
            with self.assertRaises(TaskEnd):
                self.task.run()
        self.assertEqual(self.task.current_count, 2)
        self.assertEqual(self.task._ap_completed, 2)
        self.assertEqual(self.task.forced_state, S.COURTYARD)
        self.assertTrue(self.task.scheduled[-1]['success'])
        self.assertEqual(sum(c[2]=='AP_GUJI_FIRE' for c in self.task.device.clicks), 2)
        self.assertEqual(sum(c[2]=='AP_GUJI_SETTLEMENT' for c in self.task.device.clicks), 4)

    def test_exact_three_limit_never_starts_fourth_battle(self):
        from contextlib import ExitStack
        t = self.task
        t.conf.general_climb.ap_limit = 3
        with ExitStack() as stack:
            for p in self.configure_complete_flow(): stack.enter_context(p)
            t.ap_loop()
        self.assertEqual(t.current_count,3)
        self.assertEqual(t._ap_completed,3)
        self.assertEqual(sum(c[2]=='AP_GUJI_FIRE' for c in t.device.clicks),3)

    def test_full_run_time_expires_during_battle_finish_then_stop(self):
        from contextlib import ExitStack
        from module.exception import TaskEnd
        with ExitStack() as stack:
            for p in self.configure_complete_flow(expire_in_battle=True):
                stack.enter_context(p)
            with self.assertRaises(TaskEnd):
                self.task.run()
        self.assertEqual(self.task._ap_completed, 1)
        self.assertEqual(self.task.forced_state, S.COURTYARD)
        self.assertTrue(self.task.scheduled[-1]['success'])
        self.assertEqual(sum(c[2]=='AP_GUJI_FIRE' for c in self.task.device.clicks), 1)

    def test_full_run_mandatory_resource_exhaustion_after_one_battle(self):
        from contextlib import ExitStack
        from module.exception import TaskEnd
        with ExitStack() as stack:
            for p in self.configure_complete_flow(activity=1):
                stack.enter_context(p)
            with self.assertRaises(TaskEnd):
                self.task.run()
        self.assertEqual(self.task._ap_completed, 1)
        self.assertEqual(self.task.balance, 0)
        self.assertTrue(self.task.scheduled[-1]['success'])
        self.assertTrue(self.task._ap_resource_exhausted)
        # One accepted entry and one refused entry, no repeated rejected clicks.
        self.assertEqual(sum(c[2]=='AP_GUJI_FIRE' for c in self.task.device.clicks),2)

    def test_multiple_battles_only_initial_two_five_field_ocr_groups(self):
        from contextlib import ExitStack
        t = self.task
        t.conf.general_climb.ap_limit = 4
        names = ('AP','ACTIVITY','X5','AP_COST','ACTIVITY_COST')
        values = ('33000','29','0','×6','×1')
        with ExitStack() as stack:
            # Use the real resource reader, replacing only the OCR engine calls.
            for p in self.configure_complete_flow():
                stack.enter_context(p)
            stack.enter_context(patch.object(t,'ap_read_resources',side_effect=lambda: ap.ApGujiMixin.ap_read_resources(t)))
            calls = [stack.enter_context(patch.object(getattr(t,'O_AP_GUJI_'+n),'ocr',return_value=v))
                     for n,v in zip(names,values)]
            t.ap_loop()
            self.assertEqual([c.call_count for c in calls],[2]*5)
        self.assertEqual(t.current_count,4)
        self.assertEqual(sum(c[2]=='AP_GUJI_FIRE' for c in t.device.clicks),4)
        self.assertFalse(t._x5_active)

    def test_offline_timing_stable_cycles(self):
        # Deterministic model: each frame .1 s, five-field OCR .075 s,
        # combat 2 s, instantaneous page animation and input delivery.
        # This asserts adapter overhead, never a claim about real game speed.
        from contextlib import ExitStack
        t = self.task
        t.conf.general_climb.ap_limit = 3
        waits, reads, phases = [], [], []
        def wait(seconds):
            waits.append((self.clock.now,seconds))
            self.clock.sleep(seconds)
        def read():
            reads.append(self.clock.now)
            self.clock.sleep(.075)
            return ap.ApResources(33000,t.balance,0,6,1)
        with ExitStack() as stack:
            for p in self.configure_complete_flow(): stack.enter_context(p)
            stack.enter_context(patch.object(ap,'sleep',side_effect=wait))
            stack.enter_context(patch.object(t,'ap_read_resources',side_effect=read))
            stack.enter_context(patch.object(t,'ap_trace',side_effect=lambda event,**kw: phases.append((self.clock.now,event,kw))))
            t.ap_loop()
        fires = [c[3] for c in t.device.clicks if c[2]=='AP_GUJI_FIRE']
        reward_clicks = [c[3] for c in t.device.clicks if c[2]=='AP_GUJI_SETTLEMENT' and c[4]==S.REWARD]
        victory_seen = [time for time,event,kw in phases if event=='first_seen' and kw.get('state')=='victory']
        complete = [time for time,event,kw in phases if event=='settlement_complete']
        self.assertEqual(len(reads),2)
        for fire,reward in zip(fires[1:],reward_clicks):
            self.assertGreaterEqual(fire-reward,.45+t.AP_CHALLENGE_STABLE)
            self.assertLess(fire-reward,1.7)
        for seen,done in zip(victory_seen,complete):
            self.assertAlmostEqual(done-seen,1.45)
        for start,end in zip(fires,fires[1:]):
            self.assertLessEqual(sum(seconds for time,seconds in waits if start<=time<end),2.95)

    def test_single_frame_toast_detected_before_entry_classifier(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        seen = []
        original_frame = t.device.screenshot
        clicked = [False]
        def click(name):
            clicked[0] = True
        def frame():
            original_frame()
            if clicked[0]:
                seen.append(self.clock.now)
                if len(seen) == 1:
                    t.device.image = self.insufficient_image.copy()
        t.device.on_click = click
        with patch.object(t.device,'screenshot',side_effect=frame), \
             patch.object(t,'check_tickets_enough',return_value=True), \
             patch.object(t.O_AP_GUJI_INSUFFICIENT,'ocr',return_value='琥珀卷轴不足'), \
             patch.object(t,'run_general_battle') as battle:
            self.assertFalse(t.ap_start_battle())
            battle.assert_not_called()
            self.assertFalse(t.ap_start_battle())  # Exhaustion latch.
        self.assertEqual(len(seen),1)
        self.assertEqual(len(t.device.clicks),1)
        self.assertEqual(t.current_count,0)

    def test_missed_toast_full_run_times_out_saves_and_schedules_failure(self):
        from contextlib import ExitStack
        from module.exception import TaskEnd
        t = self.task
        t.AP_ENTRY_TIMEOUT = 1
        with ExitStack() as stack:
            for p in self.configure_complete_flow(): stack.enter_context(p)
            t.device.on_click = lambda name: setattr(t,'forced_state',S.CHALLENGE)
            t.forced_state = S.CHALLENGE
            stack.enter_context(patch.object(t,'ap_insufficient',return_value=False))
            with self.assertRaises(TaskEnd): t.run()
        self.assertEqual(sum(c[2]=='AP_GUJI_FIRE' for c in t.device.clicks),1)
        self.assertEqual(t.current_count,0)
        self.assertFalse(t.scheduled[-1]['success'])
        self.assertTrue(t.saved_reason)

    def test_startup_ocr_exception_full_run_never_fires(self):
        from contextlib import ExitStack
        from module.exception import TaskEnd
        t = self.task
        with ExitStack() as stack:
            for p in self.configure_complete_flow(): stack.enter_context(p)
            stack.enter_context(patch.object(t,'ap_read_resources',side_effect=ap.ApSafetyStop('OCR failed')))
            with self.assertRaises(TaskEnd): t.run()
        self.assertFalse(any(c[2]=='AP_GUJI_FIRE' for c in t.device.clicks))
        self.assertFalse(t.scheduled[-1]['success'])

    def test_trace_has_first_seen_and_two_click_delivery_stages(self):
        t = self.task
        t.forced_state = S.VICTORY
        t.device.on_click = lambda name: setattr(t,'forced_state',S.REWARD if t.forced_state==S.VICTORY else S.CHALLENGE)
        with patch.object(t,'ap_trace') as trace:
            t.ap_battle_wait()
        self.assertEqual([c.kwargs['state'] for c in trace.call_args_list if c.args[0]=='first_seen'],
                         ['victory','reward','challenge'])
        self.assertEqual([c.kwargs['state'] for c in trace.call_args_list if c.args[0]=='settlement_delivered'],
                         ['victory','reward'])

    def test_uniform_full_screen_samples(self):
        points = [ap.sample_settlement_point(random.Random(i)) for i in range(50000)]
        self.assertTrue(all(ap.safe_settlement_point(*p) for p in points))
        self.assertGreater(len(set(points)), 47000)
        self.assertTrue(any(x < 240 and 150 <= y <= 440 for x,y in points))
        self.assertTrue(any(x > 1080 and 150 <= y <= 440 for x,y in points))
        self.assertTrue(any(240 <= x <= 1080 and y > 440 for x,y in points))
        self.assertTrue(any(x < 820 and y < 150 for x,y in points))
        # Four equal-area permitted regions: empirical counts within 8%.
        areas = [(20,20,220,120),(20,450,220,550),(1100,100,1200,300),(400,460,600,560)]
        hits = [sum(x0 <= x < x1 and y0 <= y < y1 for x,y in points) for x0,y0,x1,y1 in areas]
        self.assertLess(max(hits)/min(hits), 1.15)

    def test_no_settlement_click_without_state(self):
        t = self.task
        for state in (S.UNKNOWN, S.CHALLENGE, S.BATTLE, S.DIALOG):
            t.forced_state = state
            with self.assertRaises(ap.ApSafetyStop):
                t.ap_settlement_click(state)
        self.assertFalse(t.device.clicks)

    def test_victory_reward_return_success_single_clicks(self):
        t = self.task
        t.forced_state = S.VICTORY
        t.current_count = 1
        def click(name):
            t.forced_state = S.REWARD if t.forced_state == S.VICTORY else S.CHALLENGE
        t.device.on_click = click
        self.assertTrue(t.ap_battle_wait())
        self.assertEqual(t._ap_completed, 1)
        self.assertEqual(t.count_map['ap'], 1)
        self.assertEqual([x[4] for x in t.device.clicks], [S.VICTORY, S.REWARD])
        self.assertGreaterEqual(t.device.clicks[1][3]-t.device.clicks[0][3], t.AP_STAGE_CLICK_GAP)
        self.assertTrue(all(ap.safe_settlement_point(x,y) for x,y,*_ in t.device.clicks))

    def test_stuck_settlement_three_clicks_then_stop(self):
        t = self.task
        t.forced_state = S.VICTORY
        with self.assertRaises(ap.ApSafetyStop):
            t.ap_battle_wait()
        self.assertEqual(len(t.device.clicks), 3)
        self.assertTrue(all(b[3]-a[3] >= 2 for a,b in zip(t.device.clicks,t.device.clicks[1:])))
        self.assertFalse(getattr(t, '_ap_completed', 0))

    def test_settlement_timeout(self):
        t = self.task
        t.forced_state = S.VICTORY
        t.AP_SETTLE_TIMEOUT = .7
        with self.assertRaisesRegex(ap.ApSafetyStop, 'phase timed out'):
            t.ap_battle_wait()
        self.assertFalse(getattr(t, '_ap_completed', 0))

    def test_challenge_return_without_reward_is_not_success(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        with self.assertRaises(ap.ApSafetyStop):
            t.ap_battle_wait()

    def test_reward_without_victory_requires_explicit_recovery(self):
        t = self.task
        t.forced_state = S.REWARD
        with self.assertRaises(ap.ApSafetyStop):
            t.ap_battle_wait()
        t._ap_reward_recovery = True
        t.device.on_click = lambda name: setattr(t, 'forced_state', S.CHALLENGE)
        self.assertTrue(t.ap_battle_wait())

    def test_battle_timeout_no_input(self):
        t = self.task
        t.forced_state = S.BATTLE
        t.AP_BATTLE_TIMEOUT = 3
        with self.assertRaises(ap.ApSafetyStop):
            t.ap_battle_wait()
        self.assertFalse(t.device.clicks)

    def test_unknown_battle_animation_is_bounded_no_input(self):
        t = self.task
        t.forced_state = S.UNKNOWN
        t.AP_UNKNOWN_TIMEOUT = 2
        with self.assertRaises(ap.ApSafetyStop):
            t.ap_battle_wait()
        self.assertFalse(t.device.clicks)

    def test_slow_entry_and_delayed_detection_only_one_challenge(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        t.device.on_click = lambda name: setattr(t, 'forced_state', S.UNKNOWN)
        t.device.on_frame = lambda: setattr(t, 'forced_state', S.BATTLE) if self.clock.now >= 20 else None
        with patch.object(t, 'check_tickets_enough', return_value=True), \
             patch.object(t, 'run_general_battle', return_value=True) as battle:
            self.assertTrue(t.ap_start_battle())
            battle.assert_called_once()
        self.assertEqual(len(t.device.clicks), 1)

    def test_entry_timeout_final_frame_can_take_over_battle(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        t.AP_ENTRY_TIMEOUT = 1
        t.device.on_click = lambda name: setattr(t, 'forced_state', S.UNKNOWN)
        t.device.on_frame = lambda: setattr(t, 'forced_state', S.BATTLE) if self.clock.now >= 1.35 else None
        with patch.object(t, 'check_tickets_enough', return_value=True), \
             patch.object(t, 'run_general_battle', return_value=True) as battle:
            self.assertTrue(t.ap_start_battle())
            battle.assert_called_once()
        self.assertEqual(len(t.device.clicks), 1)

    def test_entry_timeout_never_reclicks(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        t.AP_ENTRY_TIMEOUT = 2
        with patch.object(t, 'check_tickets_enough', return_value=True):
            with self.assertRaises(ap.ApSafetyStop):
                t.ap_start_battle()
        self.assertEqual(len(t.device.clicks), 1)

    def test_button_mismatch_no_input(self):
        self.task.forced_state = S.UNKNOWN
        with self.assertRaises(ap.ApSafetyStop):
            self.task.ap_start_battle()
        self.assertFalse(self.task.device.clicks)

    def test_count_and_time_limits(self):
        t = self.task
        t.current_count = 2
        self.assertTrue(t.ap_should_stop())
        t.current_count = 0
        t.start_time -= timedelta(hours=1)
        self.assertTrue(t.ap_should_stop())
        with patch.object(t, 'check_tickets_enough', return_value=True):
            self.assertFalse(t.ap_start_battle())
        self.assertFalse(t.device.clicks)

    def test_resource_exhaustion_no_challenge(self):
        with patch.object(self.task, 'check_tickets_enough', return_value=False):
            self.assertFalse(self.task.ap_start_battle())
        self.assertFalse(self.task.device.clicks)

    def test_in_progress_battle_finishes_even_at_limit(self):
        t = self.task
        t.forced_state = S.BATTLE
        t.current_count = 2
        with patch.object(t, 'run_general_battle', return_value=True) as battle:
            self.assertTrue(t.ap_start_battle())
            battle.assert_called_once()
        self.assertFalse(t.device.clicks)

    def test_navigation_direct_home_challenge_no_intermediate(self):
        t = self.task
        t.forced_state = S.COURTYARD
        def click(name):
            t.forced_state = S.HOME if name == 'AP_GUJI_ENTRY' else S.CHALLENGE
        t.device.on_click = click
        self.assertEqual(t.ap_navigate(), S.CHALLENGE)
        self.assertEqual([x[2] for x in t.device.clicks], ['AP_GUJI_ENTRY','AP_GUJI_ROUTE'])

    def test_navigation_refresh_limit(self):
        t = self.task
        t.forced_state = S.COURTYARD
        with patch.object(t, 'ap_match', side_effect=lambda rule: rule == t.I_AP_GUJI_REFRESH), \
             patch.object(t, 'ap_click_match') as click:
            with self.assertRaises(ap.ApSafetyStop):
                t.ap_navigate()
            self.assertEqual(click.call_count, 8)

    def test_home_entry_click_limit(self):
        t = self.task
        t.forced_state = S.HOME
        with self.assertRaises(ap.ApSafetyStop):
            t.ap_navigate()
        self.assertEqual(len(t.device.clicks), 3)

    def test_mixed_sequence_rejected_before_any_input(self):
        from module.exception import TaskEnd
        t = self.task
        t.conf.general_climb.run_sequence = 'pass,ap'
        with self.assertRaises(TaskEnd):
            t.run()
        self.assertFalse(t.device.clicks)
        self.assertFalse(t.scheduled[-1]['success'])

    def test_failed_preparation_cannot_fall_through_to_battle_wait(self):
        t = self.task
        t.forced_state = S.PREPARE
        with patch.object(GeneralBattle, 'battle_before', return_value=False):
            with self.assertRaises(ap.ApSafetyStop):
                t.battle_before(None, t.get_general_battle_conf())
        self.assertIsNone(t._ap_guard_deadline)

    def test_existing_preset_helper_deadline_is_enforced(self):
        t = self.task
        t._ap_guard_deadline = 1
        self.clock.now = 2
        with self.assertRaises(ap.ApSafetyStop):
            t.screenshot()
        self.assertFalse(t.device.clicks)

    def test_public_battle_reused_with_auto_and_random_disabled(self):
        t = self.task
        t.conf.general_battle.auto_battle_enable = True
        t.conf.general_battle.enable_ap_anti_detect = True
        config = t.get_general_battle_conf()
        with patch.object(GeneralBattle, 'run_general_battle', return_value=True) as call:
            self.assertTrue(t.run_general_battle(config))
            self.assertIsNone(call.call_args.kwargs['remaining_count'])
            self.assertFalse(call.call_args.kwargs['config'].auto_battle_enable)
            self.assertFalse(call.call_args.kwargs['config'].random_click_swipt_enable)
        self.assertTrue(t.conf.general_battle.auto_battle_enable)  # Stored config unchanged.

    def test_existing_lock_and_preset_group_team_configuration_reused(self):
        t = self.task
        t.conf.general_battle.enable_ap_preset = True
        t.conf.switch_soul_config.ap_group_team = '2,3'
        config = t.get_general_battle_conf()
        self.assertFalse(config.lock_team_enable)
        self.assertTrue(config.preset_enable)
        self.assertEqual((config.preset_group,config.preset_team), (2,3))


class EntryReadinessTests(BaseCase):
    def test_first_visible_frame_waits_for_continuous_fresh_frames(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        times = []
        t.device.on_frame = lambda: times.append(self.clock.now)
        with patch.object(t,'ap_trace') as trace:
            self.assertEqual(t.ap_wait_challenge_ready(),S.CHALLENGE)
        self.assertGreaterEqual(self.clock.now-times[0],t.AP_CHALLENGE_STABLE)
        self.assertGreaterEqual(len(times),3)
        self.assertTrue(all(b>a for a,b in zip(times,times[1:])))
        self.assertFalse(t.device.clicks)
        self.assertTrue(any(c.args[0]=='challenge_ready' for c in trace.call_args_list))

    def test_visible_but_temporarily_noninteractive_page_is_not_clicked_early(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        accepted, swallowed = [], []
        def click(name):
            if self.clock.now < .85:
                swallowed.append(name)
            else:
                accepted.append(name)
                t.forced_state = S.PREPARE
        t.device.on_click = click
        with patch.object(t,'check_tickets_enough',return_value=True), \
             patch.object(t,'run_general_battle',return_value=True):
            self.assertTrue(t.ap_start_battle())
        self.assertEqual(accepted,['AP_GUJI_FIRE'])
        self.assertEqual(swallowed,[])
        self.assertGreaterEqual(t.device.clicks[0][3],t.AP_CHALLENGE_STABLE)

    def test_transition_flicker_resets_readiness_window(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        t.device.on_frame = lambda: setattr(t,'forced_state',S.UNKNOWN if .45<=self.clock.now<.7 else S.CHALLENGE)
        self.assertEqual(t.ap_wait_challenge_ready(),S.CHALLENGE)
        self.assertGreaterEqual(self.clock.now,.7+t.AP_CHALLENGE_STABLE)
        self.assertFalse(t.device.clicks)

    def test_button_position_change_resets_readiness_window(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        original = t.device.screenshot
        first_moved = []
        def frame():
            original()
            if self.clock.now>=.5:
                if not first_moved: first_moved.append(self.clock.now)
                t.device.image[597:646,1129:1217] = 0
                t.device.image[597:646,1137:1225] = self.images[2][597:646,1129:1217]
        with patch.object(t.device,'screenshot',side_effect=frame):
            self.assertEqual(t.ap_wait_challenge_ready(),S.CHALLENGE)
        self.assertGreaterEqual(self.clock.now-first_moved[0],t.AP_CHALLENGE_STABLE)
        self.assertEqual(t.I_AP_GUJI_FIRE.roi_front[0],1137)
        self.assertFalse(t.device.clicks)

    def test_unknown_x5_during_readiness_never_fires(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        t.AP_CHALLENGE_READY_TIMEOUT = 1.5
        with patch.object(t,'check_tickets_enough',return_value=True), \
             patch.object(t,'ap_x5_state',return_value=ap.X5State.UNKNOWN):
            with self.assertRaisesRegex(ap.ApSafetyStop,'continuously stable'):
                t.ap_start_battle()
        self.assertFalse(t.device.clicks)

    def test_normal_stable_page_enters_without_long_fixed_delay(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        t.device.on_click = lambda name: setattr(t,'forced_state',S.PREPARE)
        with patch.object(t,'check_tickets_enough',return_value=True), \
             patch.object(t,'run_general_battle',return_value=True) as battle, \
             patch.object(t,'ap_read_resources') as read:
            self.assertTrue(t.ap_start_battle())
            read.assert_not_called()
            battle.assert_called_once()
        self.assertLess(self.clock.now,1.6)
        self.assertEqual(len(t.device.clicks),1)

    def test_swallowed_click_unspent_resources_still_cannot_prove_no_pending_request(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        t.AP_ENTRY_TIMEOUT = 4
        before = t.device.image.copy()
        with patch.object(t,'check_tickets_enough',return_value=True), \
             patch.object(t,'run_general_battle') as battle:
            with self.assertRaises(ap.ApSafetyStop): t.ap_start_battle()
            battle.assert_not_called()
        self.assertEqual(len(t.device.clicks),1)  # Zero automatic follow-up clicks.
        self.assertEqual(t.current_count,0)
        self.assertTrue(np.array_equal(before,t.device.image))
        self.assertIn('entry_before_input',[name for name,frame in t.snapshots])
        self.assertIn('entry_pending_3s',[name for name,frame in t.snapshots])

    def test_already_charged_but_challenge_still_visible_never_reclicks(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        t.AP_ENTRY_TIMEOUT = 4
        charged = []
        original = t.device.screenshot
        t.device.on_click = lambda name: charged.append(True)
        def frame():
            original()
            if charged:
                t.device.image[12:47,772:858] = 0
                cv2.putText(t.device.image,'28',(775,37),cv2.FONT_HERSHEY_SIMPLEX,.8,(255,255,255),1)
        with patch.object(t,'check_tickets_enough',return_value=True), \
             patch.object(t.device,'screenshot',side_effect=frame), \
             patch.object(t,'run_general_battle') as battle:
            with self.assertRaises(ap.ApSafetyStop): t.ap_start_battle()
            battle.assert_not_called()
        self.assertEqual(len(t.device.clicks),1)
        self.assertEqual(t.current_count,0)
        before_frame = next(frame for name,frame in t.snapshots if name=='entry_before_input')
        pending_frame = next(frame for name,frame in t.snapshots if name=='entry_pending_3s')
        self.assertFalse(np.array_equal(before_frame[12:47,772:858],pending_frame[12:47,772:858]))

    def test_twenty_one_second_delayed_entry_is_taken_over_without_reclick(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        fired = []
        def click(name):
            fired.append(self.clock.now)
            t.forced_state = S.UNKNOWN
        def frame():
            if fired and self.clock.now-fired[0]>=21:
                t.forced_state = S.PREPARE
        t.device.on_click, t.device.on_frame = click, frame
        with patch.object(t,'check_tickets_enough',return_value=True), \
             patch.object(t,'run_general_battle',return_value=True) as battle:
            self.assertTrue(t.ap_start_battle())
            battle.assert_called_once()
        self.assertEqual(len(t.device.clicks),1)
        self.assertGreaterEqual(self.clock.now-fired[0],21)
        self.assertLess(self.clock.now-fired[0],25)

    def test_unknown_confirmation_after_single_fire_stops_without_confirming(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        t.device.on_click = lambda name: setattr(t,'forced_state',S.DIALOG)
        with patch.object(t,'check_tickets_enough',return_value=True), \
             patch.object(t,'run_general_battle') as battle:
            with self.assertRaisesRegex(ap.ApSafetyStop,'never confirm'): t.ap_start_battle()
            battle.assert_not_called()
        self.assertEqual([c[2] for c in t.device.clicks],['AP_GUJI_FIRE'])

    def test_unknown_dialog_before_ready_never_clicks(self):
        t = self.task
        t.forced_state = S.DIALOG
        with self.assertRaises(ap.ApSafetyStop): t.ap_wait_challenge_ready()
        self.assertFalse(t.device.clicks)

    def test_limit_reached_during_readiness_never_fires(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        def frame():
            if self.clock.now>=.5:
                t.conf.general_climb.ap_limit = 0
        t.device.on_frame = frame
        with patch.object(t,'check_tickets_enough',return_value=True):
            self.assertFalse(t.ap_start_battle())
        self.assertFalse(t.device.clicks)

    def test_time_limit_reached_during_readiness_never_fires(self):
        t = self.task
        t.forced_state = S.CHALLENGE
        def frame():
            if self.clock.now>=.5: t.start_time -= timedelta(hours=1)
        t.device.on_frame = frame
        with patch.object(t,'check_tickets_enough',return_value=True):
            self.assertFalse(t.ap_start_battle())
        self.assertFalse(t.device.clicks)


class RegressionTests(unittest.TestCase):
    def test_json_sources_and_generated_assets_agree(self):
        for item in json.loads((ROOT/'tasks/ActivityShikigami/as/guji_image.json').read_text(encoding='utf-8')):
            rule = getattr(Assets, 'I_'+item['itemName'].upper())
            self.assertEqual(tuple(rule.roi_back),tuple(map(int,item['roiBack'].split(','))))
            self.assertEqual(rule.threshold,item['threshold'])
            self.assertEqual(Path(rule.file).resolve(),(ROOT/'tasks/ActivityShikigami/as'/item['imageName']).resolve())
            self.assertEqual(rule.image.shape[:2],tuple(map(int,item['roiFront'].split(',')))[3:1:-1])
        for item in json.loads((ROOT/'tasks/ActivityShikigami/fire/guji_ocr.json').read_text(encoding='utf-8')):
            rule = getattr(Assets, 'O_'+item['itemName'].upper())
            self.assertEqual(rule.roi,list(map(int,item['roiFront'].split(','))))
            self.assertEqual(rule.mode.name,'SINGLE')

    def test_legacy_methods_unchanged_and_dispatch_only_guarded(self):
        path = 'tasks/ActivityShikigami/script_task.py'
        new = (ROOT/path).read_text(encoding='utf-8')
        def methods(source):
            cls = next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name == 'ScriptTask')
            return {n.name:n for n in cls.body if isinstance(n,ast.FunctionDef)}
        after = methods(new)
        self.assertEqual(len(LEGACY_METHOD_SHA256), 36)
        self.assertTrue(LEGACY_METHOD_SHA256.keys() <= after.keys())
        guarded = {'run','start_battle','battle_wait','check_tickets_enough','run_general_battle'}
        for name, expected in LEGACY_METHOD_SHA256.items():
            current = after[name]
            if name in guarded:
                body = current.body
                if name == 'run_general_battle':
                    body = [n for n in body if not (isinstance(n,ast.If) and 'getattr' in ast.unparse(n.test))]
                else:
                    self.assertIsInstance(body[0],ast.If)
                    body = body[1:]
                current = ast.FunctionDef(name=current.name,args=current.args,body=body,
                                          decorator_list=current.decorator_list,returns=current.returns,
                                          type_comment=current.type_comment)
            with self.subTest(method=name):
                self.assertEqual(legacy_ast_sha256(current), expected)

    def test_all_legacy_asset_declarations_unchanged(self):
        path = 'tasks/ActivityShikigami/assets.py'
        def declarations(source):
            cls = next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef))
            return {n.targets[0].id:legacy_ast_sha256(n)
                    for n in cls.body if isinstance(n,ast.Assign)}
        after = declarations((ROOT/path).read_text(encoding='utf-8'))
        self.assertEqual(len(LEGACY_ASSET_SHA256), 100)
        self.assertTrue(LEGACY_ASSET_SHA256.keys() <= after.keys())
        for name, expected in LEGACY_ASSET_SHA256.items():
            with self.subTest(asset=name):
                self.assertEqual(after[name], expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
