import unittest

from jetson.ask_intent import guard_answer, propose_action, summarize_boxes


def person(identifier, x=.4, y=.2, w=.2, h=.5, **extra):
    return dict(id=identifier, cls="person", conf=.9, x=x, y=y, w=w, h=h, **extra)


class ProposalTests(unittest.TestCase):
    def test_seek_targets_largest_person(self):
        boxes = [person(3, w=.1, h=.2), person(5, w=.3, h=.7), dict(id=9, cls="chair", conf=.9, x=0, y=0, w=.9, h=.9)]
        self.assertEqual(propose_action("Go to that person", boxes), {"type": "seek", "target_id": 5})
        self.assertEqual(propose_action("seek the victim", boxes), {"type": "seek", "target_id": 5})

    def test_seek_prefers_person_marked_down(self):
        boxes = [person(3, w=.3, h=.7), person(4, w=.2, h=.2, down=True)]
        self.assertEqual(propose_action("drive to the person", boxes)["target_id"], 4)

    def test_seek_needs_a_tracked_person(self):
        self.assertIsNone(propose_action("go to the person", []))
        self.assertIsNone(propose_action("go to the person", [dict(cls="person")]))

    def test_scan(self):
        self.assertEqual(propose_action("Scan the room", []), {"type": "scan"})
        self.assertEqual(propose_action("look around", []), {"type": "scan"})

    def test_questions_propose_nothing(self):
        for question in ("What do you see?", "Is that person hurt?", "Where is the exit?"):
            self.assertIsNone(propose_action(question, [person(3)]))


class SummaryTests(unittest.TestCase):
    def test_boxes_are_described_compactly(self):
        text = summarize_boxes([person(3, x=.05, w=.15, h=.7), person(4, x=.7, w=.1, h=.2, down=True)])
        for word in ("person #3", "left", "close", "person #4", "right", "far", "down"):
            self.assertIn(word, text)

    def test_empty_and_malformed(self):
        self.assertEqual(summarize_boxes([]), "nothing detected")
        self.assertEqual(summarize_boxes([dict(cls="person"), "junk"]), "nothing detected")


class GuardTests(unittest.TestCase):
    def test_first_person_motion_claims_are_removed(self):
        for claim in ("I'm heading towards the person.", "I will drive to them now.", "Moving closer to the person.",
                      "Okay, let me go over there.", "I’m approaching them."):
            self.assertEqual(guard_answer(f"One person on the left. {claim}", None), "One person on the left.")

    def test_scene_motion_is_kept(self):
        text = "The person is heading toward the door. A cart is moving. I see a person moving."
        self.assertEqual(guard_answer(text, None), text)

    def test_only_claim_becomes_neutral_and_proposals_need_confirmation(self):
        answer = guard_answer("I'm heading towards the person.", {"type": "seek", "target_id": 5})
        self.assertNotIn("heading", answer)
        self.assertTrue(answer.endswith("Confirm on the headset to seek person #5."))
        self.assertTrue(guard_answer("A hallway.", {"type": "scan"}).endswith("Confirm on the headset to scan the room."))


if __name__ == "__main__":
    unittest.main()
