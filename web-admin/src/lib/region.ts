// 한국 주소 문자열에서 지역(시/도, 시군구)을 파싱한다.
// 보픽 지역 필터링 및 고객DB 지역별 분류에 사용.

// 시/도 정규화 표(앞부분 매칭 → 짧은 표준명)
const SIDO_TABLE: { match: RegExp; name: string }[] = [
  { match: /^서울/, name: '서울' },
  { match: /^부산/, name: '부산' },
  { match: /^대구/, name: '대구' },
  { match: /^인천/, name: '인천' },
  { match: /^광주/, name: '광주' },
  { match: /^대전/, name: '대전' },
  { match: /^울산/, name: '울산' },
  { match: /^세종/, name: '세종' },
  { match: /^경기/, name: '경기' },
  { match: /^강원/, name: '강원' },
  { match: /^충청?북|^충북/, name: '충북' },
  { match: /^충청?남|^충남/, name: '충남' },
  { match: /^전라?북|^전북/, name: '전북' },
  { match: /^전라?남|^전남/, name: '전남' },
  { match: /^경상?북|^경북/, name: '경북' },
  { match: /^경상?남|^경남/, name: '경남' },
  { match: /^제주/, name: '제주' },
];

export interface RegionParts {
  sido: string;      // 시/도 (예: 서울, 경기). 미상이면 ''
  sigungu: string;   // 시/군/구 (예: 강남구, 수원시). 없으면 ''
}

export function parseRegion(address: any): RegionParts {
  const s = (address == null ? '' : String(address)).trim();
  if (!s) return { sido: '', sigungu: '' };
  const tokens = s.split(/\s+/);
  const first = tokens[0] || '';
  let sido = '';
  for (const row of SIDO_TABLE) {
    if (row.match.test(first)) { sido = row.name; break; }
  }
  // 시군구: 다음 토큰이 시/군/구로 끝나면 사용
  let sigungu = '';
  for (let i = 1; i < Math.min(tokens.length, 3); i++) {
    if (/[시군구]$/.test(tokens[i])) { sigungu = tokens[i]; break; }
  }
  return { sido, sigungu };
}
