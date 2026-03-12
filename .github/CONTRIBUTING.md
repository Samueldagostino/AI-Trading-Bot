# CI/CD Pipeline Documentation

## Overview

This project uses GitHub Actions to ensure code quality, validate trading logic, and prevent performance regressions. The CI pipeline is designed specifically for an institutional-grade MNQ futures trading bot.

## Pipeline Architecture

### 1. CI Workflow (`.github/workflows/ci.yml`)

**Trigger**: Pull requests to `main` or `develop` branches

**Runs On**: `ubuntu-latest` with Python 3.11

**Steps**:

#### Linting
- Uses `ruff` to check code style
- Validates PEP 8 compliance
- Runs on all Python source files in `nq_bot_vscode/`

#### Unit Tests
- Runs pytest with coverage reporting
- Mocks PostgreSQL database (asyncpg)
- Tests run in `nq_bot_vscode/tests/`
- Generates coverage report

#### HC Filter Constants Validation
- **Critical Step** — Validates that these constants haven't drifted:
  - `HIGH_CONVICTION_MIN_SCORE = 0.75` (signal strength gate)
  - `HIGH_CONVICTION_MAX_STOP_PTS = 30.0` (max stop distance in points)
  - `_EXPECTED_HTF_GATE = 0.3` (HTF bias strength gate)
- Fails the build if any constant has changed
- These were calibrated against 6 months of OOS data (Sep 2025 - Feb 2026)

#### PR Comment
- Posts summary of test results
- Shows validation status
- Lists strategy configuration

### 2. Backtest Validation Workflow (`.github/workflows/backtest-validation.yml`)

**Trigger**: Pull requests to `main` branch ONLY (more expensive)

**Runs On**: `ubuntu-latest` with Python 3.11

**Steps**:

#### Sample Data Generation
- Generates synthetic TradingView bar data if needed
- Creates `nq_bot_vscode/data/tradingview/mnq_sample_1m.csv`
- Allows CI to run without storing large historical data files

#### Run Backtest
- Executes `scripts/multi_period_backtest.py`
- Processes historical TradingView data
- Uses production pipeline (Variant C exit, HC filter, sweep detector)
- Outputs results to `/tmp/backtest_results.json`

#### Validate Against Baseline
- Compares results against `nq_bot_vscode/config/backtest_baseline.json`
- Baseline metrics (6-month OOS, Config D):
  - Profit Factor: 1.73
  - Win Rate: 61.9%
  - Max Drawdown: 1.4%
  - Total Trades: 1,524
  - Total P&L: $25,581

#### Regression Check
- **Fails if**:
  - Profit Factor drops below 90% of baseline (1.73 * 0.9 = 1.56)
  - Max Drawdown exceeds 110% of baseline (1.4% * 1.1 = 1.54%)
  - Win Rate drops below 90% of baseline (61.9% * 0.9 = 55.7%)

#### Generate Report & Post to PR
- Creates markdown report with:
  - Summary metrics
  - Performance breakdown (C1 vs C2 scale-out split)
  - HTF filtering impact
  - Validation status

## Constants & Validation

### Why Constant Validation Matters

The three constants validated in CI are **hard gates** that define the bot's core risk and signal quality profile:

**1. `HIGH_CONVICTION_MIN_SCORE = 0.75`**
- Minimum signal confidence score to enter a trade
- Below 0.75 = rejected as noise
- Dropping this: silently reduces win rate and increases drawdown
- Raising this: may miss valid setups, reduce trade count

**2. `HIGH_CONVICTION_MAX_STOP_PTS = 30.0`**
- Maximum stop distance in points from entry
- Caps tail risk per trade
- If loosened to 35pts: expected loss increases ~15%, profit factor declines
- If tightened to 25pts: fewer setups qualify, lower trade count

**3. `_EXPECTED_HTF_GATE = 0.3`**
- HTF bias strength threshold (Config D, Feb 2026 validation)
- Higher timeframe consensus must be >= 0.3 to allow entry
- If changed to 0.7: profit factor degrades from 1.29 to 0.79 (silent catastrophe!)
- Config D is the ONLY validated configuration

### Running Validation Locally

```bash
# Validate constants without committing
python nq_bot_vscode/scripts/ci_validate_constants.py

# Expected output:
# HIGH_CONVICTION_MIN_SCORE = 0.75 ✓
# HIGH_CONVICTION_MAX_STOP_PTS = 30.0 ✓
# _EXPECTED_HTF_GATE = 0.3 ✓
# RESULT: All constants validated ✓
```

## Backtest Baseline & Regression Testing

### Baseline Snapshot

```
Period: 2025-09 to 2026-02 (6 months OOS)
Data Source: FirstRate 1m bars
Configuration: Config D + Variant C exit + Sweep Detector
Account: $50,000

Metrics:
  Total Trades: 1,524
  Profit Factor: 1.73
  Win Rate: 61.9%
  Max Drawdown: 1.4%
  Total P&L: $25,581
  Monthly Trades: 254 avg
  Expectancy/Trade: $16.79

Scale-Out Split:
  C1 (Trail): $10,008 (39%)
  C2 (Runner): $15,573 (61%)
```

### Regression Thresholds

The backtest-validation workflow uses conservative thresholds to catch performance degradation:

| Metric | Threshold | Interpretation |
|--------|-----------|-----------------|
| Profit Factor | >= 90% of baseline | Must stay above 1.56 |
| Max Drawdown | <= 110% of baseline | Must stay below 1.54% |
| Win Rate | >= 90% of baseline | Must stay above 55.7% |

### Running Backtest Locally

```bash
# Run backtest with sample data
cd nq_bot_vscode
python ../scripts/multi_period_backtest.py \
  --data-dir ./data/tradingview \
  --output-file /tmp/results.json

# Validate against baseline
python ../scripts/validate_backtest_results.py \
  --baseline ./config/backtest_baseline.json \
  --results /tmp/results.json

# Check for regression
python ../scripts/check_backtest_regression.py \
  --baseline ./config/backtest_baseline.json \
  --results /tmp/results.json
```

## Helper Scripts

All helper scripts are in `/scripts/` at repo root:

### `ci_validate_constants.py`
- **Location**: `nq_bot_vscode/scripts/ci_validate_constants.py`
- **Purpose**: Parse main.py and assert HC constants match expected values
- **Usage**: `python nq_bot_vscode/scripts/ci_validate_constants.py`
- **Exit Code**: 0 if all pass, 1 if drift detected

### `generate_sample_backtest_data.py`
- **Purpose**: Generates synthetic MNQ bar data for CI testing
- **Usage**: `python scripts/generate_sample_backtest_data.py`
- **Output**: `nq_bot_vscode/data/tradingview/mnq_sample_1m.csv`

### `validate_backtest_results.py`
- **Purpose**: Compares backtest results against baseline metrics
- **Usage**: `python scripts/validate_backtest_results.py --baseline <file> --results <file>`
- **Exit Code**: 0 if validation passes, 1 if metrics fail thresholds

### `check_backtest_regression.py`
- **Purpose**: Strict regression check (fails if performance drops too much)
- **Usage**: `python scripts/check_backtest_regression.py --baseline <file> --results <file>`
- **Thresholds**: PF >= 90%, DD <= 110%, WR >= 90%
- **Exit Code**: 0 if pass, 1 if regression detected

### `generate_backtest_report.py`
- **Purpose**: Generates markdown report for GitHub PR comments
- **Usage**: `python scripts/generate_backtest_report.py --baseline <file> --results <file> --output <file>`
- **Output**: Formatted markdown with tables and metrics

## Branch Strategy

```
main
  ↑ (only through PR)
  ├── backtest-validation.yml (runs on PR to main)
  └── ci.yml (always runs)

develop
  ↑ (only through PR)
  └── ci.yml (always runs)

feature/* and backtest/*
  └── ci.yml (always runs, no backtest validation)
```

**Why**:
- `main` = production code, backtest validation required
- `develop` = active development, unit tests sufficient
- Feature branches = no backtest burden until PR to develop/main

## Common Issues & Troubleshooting

### "HC Filter Constants Validation Failed"

**Cause**: You modified a constant in `main.py`

**Fix**: Only modify constants if you've done a full 6-month OOS backtest validation. Reset the constant:

```bash
git diff nq_bot_vscode/main.py  # See what changed
git checkout nq_bot_vscode/main.py  # Restore
```

Then file an issue if you believe a constant needs updating.

### "Backtest Profit Factor Regression"

**Cause**: Code changes degraded performance

**Common Culprits**:
- Changed signal aggregation logic
- Modified HTF bias engine
- Altered risk engine thresholds
- Changed scale-out exit conditions

**Debug**:
```bash
# Run backtest with logging
cd nq_bot_vscode
python ../scripts/multi_period_backtest.py \
  --data-dir ./data/tradingview \
  --output-file /tmp/results.json \
  --verbose

# Compare results side-by-side
python ../scripts/check_backtest_regression.py \
  --baseline ./config/backtest_baseline.json \
  --results /tmp/results.json
```

### "Tests Fail: Import Error"

**Cause**: Missing test dependencies

**Fix**:
```bash
pip install -r nq_bot_vscode/requirements
pip install pytest pytest-asyncio pytest-cov ruff
```

## Updating the Baseline

When you've improved the strategy and want to update the baseline:

1. **Run extended backtest** (6+ months of OOS data minimum)
2. **Validate metrics** are consistent across periods
3. **Update** `nq_bot_vscode/config/backtest_baseline.json` with new values
4. **Commit baseline change** separately from code changes
5. **Document reason** in commit message (e.g., "Update baseline: improved sweep detector accuracy")

### Baseline Update Template

```json
{
    "_comment": "Updated YYYY-MM after [reason]. Data: [period]. Backtest: [script]",
    "profit_factor": X.XX,
    "win_rate_pct": XX.X,
    "trades_per_month": XXX,
    "expectancy_per_trade": XX.XX,
    "max_drawdown_pct": X.X,
    "total_pnl": XXXXX,
    "monthly": [...]
}
```

## GitHub Actions Settings

### Required Secrets

None — the pipeline only uses publicly available data and baseline files.

### Recommended Workflow Settings

1. **Status Checks**: Require CI to pass before merging to `main`
2. **Dismissals**: Allow code owner dismissal of stale reviews
3. **Require Branches**: Require branches be updated before merging
4. **Auto-merge**: Disable (manual merge preferred for trading bot)

### Protection Rules for `main`

```
Require PR reviews: 1
Require status checks to pass:
  - test (ci.yml)
  - backtest (backtest-validation.yml)
Require branches to be up to date: Yes
Restrict pushes: No (only allow PRs)
```

## Performance Notes

- **CI Workflow**: ~2-3 minutes (lint + tests)
- **Backtest Workflow**: ~5-10 minutes (depends on data size)
- Backtest only runs on PRs to `main` to save compute time

## Future Enhancements

Potential improvements to the CI pipeline:

- [ ] Add out-of-sample walk-forward validation
- [ ] Monthly baseline recalibration
- [ ] Multi-period backtest aggregation report
- [ ] Performance dashboard in GitHub Pages
- [ ] Automated constant suggestion based on historical drift
- [ ] Alert on regime changes in historical data
- [ ] Integration with Discord for build notifications

## Questions?

Refer to:
- Main strategy docs: `/docs/`
- Baseline data: `nq_bot_vscode/config/backtest_baseline.json`
- Constants definition: `nq_bot_vscode/main.py` (lines 45-83)
- Validation script: `nq_bot_vscode/scripts/ci_validate_constants.py`
