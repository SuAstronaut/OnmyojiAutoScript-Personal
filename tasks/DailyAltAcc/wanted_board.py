"""Recognition of the four-card wanted board introduced on 2026-10-28.

Only reads the supplied 1280x720 RGB frame. No capture, input, OCR, or session
state. Each card owns its marker, invite label, and reward search regions.
"""
import copy

from module.atom.image import RuleImage


def _board_rule(name, front, back, threshold=0.85):
    return RuleImage(roi_front=front, roi_back=back, threshold=threshold,
                     method="Template matching",
                     file=f"./tasks/DailyAltAcc/cooperation_v2/{name}.png")


class WantedBoardV2:
    CARD_X = (90, 373, 655, 938)
    TITLE = _board_rule('title', (572, 35, 145, 43), (563, 28, 165, 58), 0.9)
    CLOSE = _board_rule('close', (1218, 72, 36, 38), (1203, 56, 67, 72))
    REFRESH = _board_rule('refresh', (552, 651, 105, 26), (542, 643, 125, 42))
    # Keep the glyph and diamond interior; its outer edge can cover character art.
    NORMAL = _board_rule('normal', (312, 119, 27, 31), (288, 106, 62, 65))
    REAL = _board_rule('real', (312, 119, 27, 31), (288, 106, 62, 65))
    INVITE = _board_rule('invite', (199, 403, 95, 28), (130, 388, 190, 58), 0.82)
    # The '?' in a rendered shard reward also covers non-cooperation mystery
    # cards. This is a loading signal only, never a cooperation/reward type.
    FRAGMENT_LOADED = _board_rule('fragment_loaded', (725, 553, 28, 36),
                                  (135, 530, 80, 78))
    DETAIL_SELF = _board_rule('detail_self', (646, 166, 111, 31), (635, 155, 140, 55))
    DETAIL_GOTO = _board_rule('detail_goto', (1056, 283, 94, 40), (1045, 270, 120, 65))
    DETAIL_CLOSE = _board_rule('detail_close', (1186, 109, 41, 40), (1170, 96, 70, 70))
    # Separate instances: probes must not mutate the old WantedQuests assets.
    # Gold comes last because other cooperation rewards can include gold too.
    TYPE_RULES = {
        name: RuleImage(roi_front=(0, 0, width, height),
                        roi_back=(110, 526, 225, 83), threshold=0.8,
                        method="Template matching",
                        file=f"./tasks/WantedQuests/invite/wq_cooperation_type_{name}.png")
        for name, width, height in (
            ('jade', 73, 52), ('dog_food', 60, 50), ('cat_food', 68, 57),
            ('sushi', 70, 51), ('gold', 72, 51))
    }

    @staticmethod
    def _probe(rule, image, roi=None):
        # Load reusable pixels once, but keep match coordinates local to a call.
        rule.load_image()
        probe = copy.copy(rule)
        probe.roi_front = list(rule.roi_front)
        if roi is not None:
            probe.roi_back = roi
        return probe if probe.match(image) else None

    @classmethod
    def is_open(cls, image):
        return bool(image is not None and getattr(image, 'shape', ()) == (720, 1280, 3)
                    and cls._probe(cls.TITLE, image)
                    and cls._probe(cls.CLOSE, image))

    @classmethod
    def is_detail(cls, image):
        # Only the book's own target prefix, location button and close count;
        # monster names and the visible wanted board behind it are not anchors.
        return bool(image is not None and getattr(image, 'shape', ()) == (720, 1280, 3)
                    and cls._probe(cls.DETAIL_SELF, image)
                    and cls._probe(cls.DETAIL_GOTO, image)
                    and cls._probe(cls.DETAIL_CLOSE, image))

    @classmethod
    def _reward_type(cls, image, slot):
        roi = (cls.CARD_X[slot] + 20, 526, 225, 83)
        for name, rule in cls.TYPE_RULES.items():
            probe = cls._probe(rule, image, roi)
            if probe is not None:
                return name
        return None

    @classmethod
    def ready(cls, image):
        # The board loads atomically (confirmed by the user): one cooperation
        # marker proves it is loaded. Without cooperation, any rendered reward
        # suffices. A title/footer around four blank cards is still not ready.
        if not cls.is_open(image) or not cls._probe(cls.REFRESH, image):
            return False
        for slot, left in enumerate(cls.CARD_X):
            marker_roi = (left + 198, 106, 62, 65)
            if (cls._probe(cls.NORMAL, image, marker_roi)
                    or cls._probe(cls.REAL, image, marker_roi)
                    or cls._card_loaded(image, slot)):
                return True
        return False

    @classmethod
    def _card_loaded(cls, image, slot):
        # Loaded content is independent of our cooperation reward taxonomy.
        return bool(cls._reward_type(image, slot) is not None
                    or cls._probe(cls.FRAGMENT_LOADED, image,
                                  (cls.CARD_X[slot] + 45, 530, 80, 78)))

    @classmethod
    def scan(cls, image):
        """Return visible cooperation cards, including an unclassified invite.

        Both kinds have '邀请好友'; only 协/享 decide ordinary versus real-world.
        Missing/conflicting markers stay None so callers cannot archive them as
        ordinary cooperation or silently report an empty board.
        """
        if not cls.ready(image):
            raise ValueError('Wanted v2 board is not ready')
        cards = []
        for slot, left in enumerate(cls.CARD_X):
            marker_roi = (left + 198, 106, 62, 65)
            normal = cls._probe(cls.NORMAL, image, marker_roi)
            real = cls._probe(cls.REAL, image, marker_roi)
            invite = cls._probe(cls.INVITE, image, (left + 40, 388, 190, 58))
            if normal is None and real is None and invite is None:
                continue
            cards.append(dict(slot=slot,
                              real=(real is not None) if (normal is None) != (real is None) else None,
                              type_name=cls._reward_type(image, slot), inviteBtn=invite))
        return cards

    @classmethod
    def target_rois(cls, slot):
        left = cls.CARD_X[slot] + 20
        return dict(discoverer=(left, 438, 225, 25),
                    friend=(left, 487, 225, 25), monster=(left, 460, 225, 25))
