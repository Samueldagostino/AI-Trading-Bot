# GitHub Actions CI/CD Pipeline - Setup Complete

## Deliverables Summary

A complete institutional-grade CI/CD pipeline has been created for the MNQ futures trading bot. This ensures code quality, prevents performance regressions, and validates critical trading constants.

### Files Created

#### 1. Workflow Files (`.github/workflows/`)

**`ci.yml`** — Main CI Pipeline
- Triggers on PRs to `main` and `develop` branches
- Runs: Python 3.11 on ubuntu-latest
- Steps:
  - Install dependencies from `requirements` file
  - Run linting with `ruff` (PEP 8 checks)
  - Run pytest with coverage
  - **Validate HC Filter Constants** (critical check)
  - Post summary comment to PR
- Time: ~2-3 minutes

**`backtest-validation.yml`** — Expensive Backtest Validation
- Triggers on PRs to `main` branch ONLY
- Steps:
  - Generate sample TradingView data
  - Run multi-timeframe backtest
  - Validate against baseline metrics
  - Generate markdown report
  - Post results to PR
  - Fail if metrics regress
- Time: ~5-10 minutes
- Includes regression thresholds:
  - Profit Factor: >= 1.56 (90% of baseline 1.73)
  - Max Drawdown: <= 1.54% (110% of baseline 1.4%)
  - Win Rate: >= 55.7% (90% of baseline 61.9%)

#### 2. Validation Script (`nq_bot_vscode/scripts/`)

**`ci_validate_constants.py`** — HC Filter Validation
- Parses `main.py` for three critical constants
- Asserts they match expected values:
  - `HIGH_CONVICTION_MIN_SCORE = 0.75`
  - `HIGH_CONVICTION_MAX_STOP_PTS = 30.0`
  - `_EXPECTED_HTF_GATE = 0.3`
- Exits non-zero if drift detected
- Prevents silent performance degradation

#### 3. Helper Scripts (`scripts/`)

**`generate_sample_backtest_data.py`** — Sample Data Generator
- Creates synthetic TradingView bar data for CI
- Generates `nq_bot_vscode/data/tradingview/mnq_sample_1m.csv`
- Allows backtest without storing large files in repo

**`validate_backtest_results.py`** — Results Validator
- Compares backtest output against baseline
- Validates key metrics within acceptable ranges
- Used by CI to ensure quality thresholds

**`check_backtest_regression.py`** — Strict Regression Check
- Fails if Profit Factor, Max Drawdown, or Win Rate regress
- Conservative thresholds (90% PF, 110% DD, 90% WR)
- Gates PRs from merging if performance drops

**`generate_backtest_report.py`** — Report Generator
- Creates formatted markdown for GitHub PR comments
- Shows metrics, comparison to baseline, scale-out split
- Includes validation status and insights

#### 4. Documentation

**`.github/CONTRIBUTING.md`** — Comprehensive Pipeline Guide
- Pipeline architecture overview
- Constants validation rationale
- Baseline snapshot and regression thresholds
- Helper script documentation
- Branch strategy
- Troubleshooting guide
- Instructions for updating baseline

**`.github/PIPELINE_SETUP.md`** — This file

---

## Key Features

### 1. HC Filter Constants Validation

**Why This Matters**:
The three validated constants define the bot's core risk and signal quality:

- `HIGH_CONVICTION_MIN_SCORE = 0.75` — Eliminates low-conviction noise
- `HIGH_CONVICTION_MAX_STOP_PTS = 30.0` — Caps tail risk per trade
- `_EXPECTED_HTF_GATE = 0.3` — Config D validated gate (critical!)

These were calibrated from 6 months of OOS backtest data (Sep 2025 - Feb 2026) and must never drift without full validation.

**Impact of Changes**:
- Loosening stop distance from 30→35pts: PF drops ~0.2, DD increases
- Lowering signal threshold from 0.75→0.70: WR drops 3-5%, more noise
- Changing gate from 0.3→0.7: **Profit Factor collapses from 1.29 to 0.79**

### 2. Backtest Validation Against Baseline

The pipeline enforces that code changes don't degrade performance:

```
Baseline (6-month OOS, Config D + Variant C):
  Profit Factor:    1.73 (min acceptable: 1.56)
  Max Drawdown:     1.4% (max acceptable: 1.54%)
  Win Rate:         61.9% (min acceptable: 55.7%)
  Total Trades:     1,524
  Total P&L:        $25,581
  Expectancy/Trade: $16.79
```

### 3. Two-Tier CI Strategy

**Tier 1: Fast (Always)**
- Linting with ruff
- Unit tests with pytest
- HC constants validation
- Time: ~2-3 min

**Tier 2: Comprehensive (PRs to main only)**
- Multi-timeframe backtest
- Regression check against baseline
- Performance report generation
- Time: ~5-10 min
- Reason: More expensive, only needed for production PRs

### 4. PR Comments

Both workflows post detailed comments to PRs:

**CI Comment**:
- Python version, OS
- Test execution status
- Validation check results
- Strategy configuration

**Backtest Comment**:
- Performance metrics (actual vs baseline)
- Scale-out breakdown (C1 vs C2 split)
- HTF filtering impact
- Validation pass/fail status

---

## Verification

All components have been tested and validated:

```bash
✓ ci.yml syntax validated (YAML)
✓ backtest-validation.yml syntax validated (YAML)
✓ ci_validate_constants.py - All constants pass (0.75, 30.0, 0.3)
✓ generate_sample_backtest_data.py - Generated 1000 sample bars
✓ validate_backtest_results.py - Validation logic tested
✓ check_backtest_regression.py - Regression thresholds validated
✓ generate_backtest_report.py - Report generation tested
```

---

## Quick Start

### For Developers

1. **Create a feature branch**:
   ```bash
   git checkout -b feature/my-improvement
   ```

2. **Make changes** and push to GitHub

3. **Open a PR to `develop`** (unit tests run automatically)

4. **When ready for production**, open a PR to `main` (both unit tests + backtest run)

5. **Check PR comments** for:
   - Test results
   - HC constants validation
   - Backtest performance vs baseline

### To Run Validation Locally

```bash
# Validate constants
python nq_bot_vscode/scripts/ci_validate_constants.py

# Run tests
cd nq_bot_vscode && pytest tests/ -v

# Generate sample data
python scripts/generate_sample_backtest_data.py

# Run backtest
python scripts/multi_period_backtest.py \
  --data-dir nq_bot_vscode/data/tradingview

# Validate results
python scripts/validate_backtest_results.py \
  --baseline nq_bot_vscode/config/backtest_baseline.json \
  --results /tmp/backtest_results.json

# Check regression
python scripts/check_backtest_regression.py \
  --baseline nq_bot_vscode/config/backtest_baseline.json \
  --results /tmp/backtest_results.json
```

---

## Configuration

### Branch Protection Rules for `main`

Recommended GitHub settings:

1. **Require status checks to pass**:
   - `test` (from ci.yml)
   - `backtest` (from backtest-validation.yml)

2. **Require branches be updated before merging**: Yes

3. **Require code reviews**: 1 approval

4. **Require PR to be up-to-date**: Yes

### Secrets & Variables

None required. The pipeline uses:
- Public baseline data: `nq_bot_vscode/config/backtest_baseline.json`
- Generated sample data: `nq_bot_vscode/data/tradingview/`
- No API keys or credentials needed

---

## Maintenance

### Monthly Tasks

- **Review baseline metrics**: Check if strategy still performs as expected
- **Monitor regression threshold hits**: Investigate any near-threshold failures
- **Update sample data**: Rotate to recent historical period if needed

### Quarterly Tasks

- **Recalibrate baseline**: After 6+ months of new trading data
- **Review HC constants**: Validate against latest OOS results
- **Audit regression thresholds**: Adjust if strategy has improved

### When Updating Baseline

1. Run 6+ month extended backtest
2. Verify metrics consistency across periods
3. Update `nq_bot_vscode/config/backtest_baseline.json`
4. Commit baseline separately with clear message
5. Document reason (e.g., "Improved sweep detector")

---

## Troubleshooting

### "Workflow not triggering"
- Check branch name (must be exact: `main`, `develop`)
- Verify `.github/workflows/` files are on main branch
- Confirm file permissions (should be readable)

### "CI passes locally but fails on GitHub"
- Python version mismatch: Use Python 3.11
- Missing dependencies: `pip install -r nq_bot_vscode/requirements`
- Test isolation: Ensure tests mock database

### "Backtest results timeout"
- Sample data may be too large
- Reduce data period or trades per backtest
- Check machine resources in GitHub Actions

### "HC constants validation fails"
- You modified a constant in `main.py`
- Reset with `git checkout nq_bot_vscode/main.py`
- Only change if full 6-month validation done

---

## File Structure

```
AI-Trading-Bot/
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                          ← Main CI pipeline
│   │   └── backtest-validation.yml         ← Backtest validation
│   ├── CONTRIBUTING.md                     ← Detailed guide
│   └── PIPELINE_SETUP.md                   ← This file
├── scripts/
│   ├── ci_validate_constants.py            ← Constants validation
│   ├── generate_sample_backtest_data.py    ← Sample data
│   ├── validate_backtest_results.py        ← Results validation
│   ├── check_backtest_regression.py        ← Regression check
│   └── generate_backtest_report.py         ← Report generation
├── nq_bot_vscode/
│   ├── main.py                             ← HC constants defined here
│   ├── config/
│   │   ├── settings.py
│   │   └── backtest_baseline.json          ← Baseline metrics
│   ├── scripts/
│   │   └── ci_validate_constants.py        ← Same as scripts/ but for import
│   ├── tests/                              ← Unit tests
│   ├── requirements                        ← Dependencies
│   └── data/tradingview/                   ← Backtest data
└── ...
```

---

## Performance Baseline

For reference, the validated baseline represents 6 months of trading data:

```json
{
    "profit_factor": 1.73,
    "win_rate_pct": 61.9,
    "trades_per_month": 254,
    "expectancy_per_trade": 16.79,
    "max_drawdown_pct": 1.4,
    "total_pnl": 25581.00,
    "c1_pnl": 10008.00,
    "c2_pnl": 15573.00,
    "total_trades": 1524,
    "account_size": 50000
}
```

The CI pipeline uses 90% of each metric as the minimum acceptable threshold.

---

## Support & Questions

Refer to:
- **Pipeline Overview**: `.github/CONTRIBUTING.md`
- **Strategy Logic**: `nq_bot_vscode/main.py` (lines 45-83 for constants)
- **Configuration**: `nq_bot_vscode/config/settings.py`
- **Baseline Data**: `nq_bot_vscode/config/backtest_baseline.json`

For constant-related questions, see the comments in `main.py` starting at line 45 (HIGH-CONVICTION FILTER section).

---

**Pipeline Created**: 2026-03-06
**Status**: Ready for Production
**Python Version**: 3.11+
**Platform**: Ubuntu-latest (GitHub Actions)
