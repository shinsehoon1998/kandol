"""2026-10-28 추가 — 엑셀 연락처를 KB 수집 고객에 붙이는 규칙(match_contacts) 테스트.

보픽 FB-0019: 예전 규칙은 (이름, 생년월일)이 안 맞으면 이름만 같은 첫 연락처를 붙였다.
그래서 동명이인 B 의 KB 기록(생년월일·계약)에 A 의 전화·주소가 붙어 보픽으로 갔고,
상담사는 A 에게 전화하며 B 의 보험계약을 봤다. 이름·번호는 전부 합성이다.

    python3 -m unittest tests/test_contact_match.py
"""
import unittest

from solting_auto.kb_crawler import match_contacts


def contact(name, birth, phone, address=""):
    return {"customer_name": name, "birth": birth, "phone": phone, "address": address}


def kb(name, birth):
    return {"customer_name": name, "birth": birth, "phone": None, "address": None}


class MatchContactsTest(unittest.TestCase):
    def test_fb0019_namesake_gets_nothing(self):
        # 엑셀엔 1953년생 김가나(번호 A)만, KB 엔 1965년생 김가나와 1953년생 김가나가 둘 다 있다.
        contacts = [contact("김가나", "530817", "010-0000-0001", "수원")]
        results = [kb("김가나", "650320"), kb("김가나", "530817")]
        n = match_contacts(contacts, results)
        self.assertEqual(n, 1)
        self.assertIsNone(results[0]["phone"], "생년월일이 다른 동명이인에게 번호가 붙었다")
        self.assertIsNone(results[0]["address"])
        self.assertEqual(results[1]["phone"], "010-0000-0001")
        self.assertEqual(results[1]["address"], "수원")

    def test_both_births_known_and_different_never_match(self):
        # 진짜 주인이 KB 에 없어도, 양쪽 생년월일이 다 있고 다르면 다른 사람이다.
        contacts = [contact("김가나", "530817", "010-0000-0001")]
        results = [kb("김가나", "650320")]
        self.assertEqual(match_contacts(contacts, results), 0)
        self.assertIsNone(results[0]["phone"])

    def test_kb_birth_unreadable_unique_name_matches(self):
        contacts = [contact("이다라", "600101", "010-0000-0002")]
        results = [kb("이다라", "")]
        self.assertEqual(match_contacts(contacts, results), 1)
        self.assertEqual(results[0]["phone"], "010-0000-0002")

    def test_kb_birth_age_prefixed_uses_tail(self):
        # "70560424" = 나이 70 + 생년월일 560424 가 붙은 수집 오류. 뒤 6자리로 정확히 맞춘다 —
        # 동명이인이 있어도 이름만으로 붙이지 않고 생년월일로 가린다.
        contacts = [contact("박마바", "560424", "010-0000-0003"), contact("박마바", "600101", "010-0000-0011")]
        results = [kb("박마바", "70560424")]
        self.assertEqual(match_contacts(contacts, results), 1)
        self.assertEqual(results[0]["phone"], "010-0000-0003")

    def test_kb_birth_unparseable_8_digits_is_unknown(self):
        # 뒤 6자리도 날짜 모양이 아니면 모름 — 한쪽 생년월일이 없는 셈이라 이름이 양쪽에서 한 명뿐일 때만 붙는다.
        contacts = [contact("서더러", "560424", "010-0000-0012")]
        results = [kb("서더러", "12999999")]
        self.assertEqual(match_contacts(contacts, results), 1)
        contacts.append(contact("서더러", "600101", "010-0000-0013"))
        results = [kb("서더러", "12999999")]
        self.assertEqual(match_contacts(contacts, results), 0, "동명이인이 있으면 모르는 생년월일로는 안 붙인다")

    def test_excel_without_jumin_unique_name_matches(self):
        contacts = [contact("최사아", "", "010-0000-0004")]
        results = [kb("최사아", "580915")]
        self.assertEqual(match_contacts(contacts, results), 1)

    def test_two_namesakes_in_excel_without_birth_is_ambiguous(self):
        contacts = [contact("정자차", "", "010-0000-0005"), contact("정자차", "", "010-0000-0006")]
        results = [kb("정자차", "610101")]
        self.assertEqual(match_contacts(contacts, results), 0)
        self.assertIsNone(results[0]["phone"])

    def test_two_namesakes_in_kb_name_only_is_ambiguous(self):
        contacts = [contact("한카타", "", "010-0000-0007")]
        results = [kb("한카타", "590101"), kb("한카타", "")]
        self.assertEqual(match_contacts(contacts, results), 0)

    def test_exact_match_beats_namesakes(self):
        contacts = [contact("윤파하", "620101", "010-0000-0008"), contact("윤파하", "640202", "010-0000-0009")]
        results = [kb("윤파하", "640202"), kb("윤파하", "620101")]
        self.assertEqual(match_contacts(contacts, results), 2)
        self.assertEqual(results[0]["phone"], "010-0000-0009")
        self.assertEqual(results[1]["phone"], "010-0000-0008")

    def test_eight_digit_birth_from_excel_is_normalized(self):
        contacts = [contact("오거너", "19530817", "010-0000-0010")]
        results = [kb("오거너", "530817")]
        self.assertEqual(match_contacts(contacts, results), 1)


if __name__ == "__main__":
    unittest.main()
