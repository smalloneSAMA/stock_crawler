import sys
with open('t_strategy_analyzer.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_start = 'def _detect_trend_phases(prices: List[float], threshold_pct: float = 3.0) -> List[int]:'
old_end = '\n\ndef generate_consolidated_signal_excel('

new_func = '''
def _detect_trend_phases(prices: List[float], threshold_pct: float = 0.5) -> List[int]:
    """
    Based on MA5 to detect trend phases

    Logic:
      1. Calculate MA5 for each position
      2. Compare current MA5 with MA5 from 5 periods ago
      3. Change > threshold -> up trend (light red)
      4. Change < -threshold -> down trend (light green)
      5. Otherwise -> sideways (white)

    Returns:
        [4, 4, None, 5, ...]  4=up(light red)  5=down(light green)  None=sideways
    """
    if len(prices) < 6:
        return [None] * len(prices)

    # Calculate MA5 (prices from old to new)
    mas = []
    for i in range(len(prices)):
        if i >= 4:
            ma = sum(prices[i-4:i+1]) / 5
            mas.append(round(ma, 4))
        else:
            mas.append(None)

    # Determine trend by comparing MA5 change over 5 periods
    fills = []
    for i in range(len(prices)):
        if mas[i] is None:
            fills.append(None)
            continue

        prev_idx = max(0, i - 5)
        if mas[prev_idx] is None:
            fills.append(None)
            continue

        change_pct = (mas[i] - mas[prev_idx]) / mas[prev_idx] * 100

        if change_pct > threshold_pct:
            fills.append(4)   # up trend
        elif change_pct < -threshold_pct:
            fills.append(5)   # down trend
        else:
            fills.append(None)  # sideways

    return fills

def generate_consolidated_signal_excel('

idx_start = content.find(old_start)
idx_end = content.find(old_end, idx_start)

if idx_start >= 0 and idx_end >= 0:
    # Replace from old function def to (but not including) the blank line before generate_consolidated_signal_excel
    content = content[:idx_start] + new_func + content[idx_end + 1:]
    with open('t_strategy_analyzer.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('OK - replaced successfully')
else:
    print(f'Not found: start={idx_start}, end={idx_end}')
