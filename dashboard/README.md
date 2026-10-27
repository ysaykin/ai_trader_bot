# Lumibot Trading Dashboard

The interactive Streamlit dashboard (`dashboard/app.py`) provides real-time mission control, capital safety oversight, and forward-testing performance tracking for the Lumibot trading platform.

For the comprehensive documentation on the dashboard mission, UI architecture, and all supported features, see:
- 📖 **[DASHBOARD.md](../DASHBOARD.md)**: Full UI specification, architecture, risk gauges, 20-grid incubator, neural policy tab, and operational runbook.
- 📋 **[USER_FEATURES.md](../USER_FEATURES.md)**: Master User Features Catalog across the entire engine and UI.

### Quick Start

```bash
# Build and run the dashboard container
docker compose up -d --build lumibot-dashboard

# Access the interface
open http://localhost:8501
```
