#!/usr/bin/env bash
# ============================================================
# Binance Quant Bot - Terminal Ön Planda Çalıştırma Scripti
# Laptop kapağı kapatılsa bile uykuyu engeller (systemd-inhibit)
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================================"
echo "🚀 BINANCE QUANT BOT BAŞLATILIYOR (ÖN PLAN / TERMINAL)"
echo "🔒 Kapak Koruması: AKTİF (Kapağı kapatsan da uykuya geçmez)"
echo "📺 Canlı log akışı başlıyor... Durdurmak için: CTRL + C"
echo "============================================================"

systemd-inhibit --what=handle-lid-switch:sleep:idle \
                --why="Binance Quant Paper Trading Bot" \
                ./.venv/bin/python bot.py
