def fmt_money_kop(v: int) -> str:
    rub = v // 100
    kop = v % 100
    return f"{rub} ₽ {kop:02d}"
