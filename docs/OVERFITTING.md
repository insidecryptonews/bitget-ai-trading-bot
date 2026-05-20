# Overfitting And Statistical Validity

Backtests are research artifacts, not proof that a strategy will make money live. A strong in-sample curve can come from chance, bad labels, duplicated data, cost mistakes, or lookahead.

## Common Failure Modes

- **Lookahead bias:** using information that was only known after entry. MFE, MAE, final labels, and final path summaries must not be used as realized return or as entry-selection features.
- **Survivorship bias:** testing only setups that are known to have moved well, such as filtering a breakout benchmark by future MFE.
- **Parameter overfitting:** tuning exact thresholds until one window looks good.
- **Perfect-looking curves:** usually suspicious until rolling walk-forward validates out of sample.
- **Inflated Sharpe:** annualization must match the return timeframe. Unknown timeframe means unknown Sharpe.
- **Profit factor infinity:** if there are no losses in a tiny sample, that is not strong edge; it is insufficient evidence.

## Minimum Evidence Before Any Live Discussion

- Clean data and label quality marked OK.
- At least 500 trades for any strong conclusion.
- Rolling walk-forward out-of-sample positive.
- OOS profit factor at least 70% of in-sample profit factor.
- Net EV positive after USDT-M futures fees, slippage, and funding when applicable.
- Return coverage OK; MFE/MAE must not substitute realized return.
- Execution safety OK.
- Sustained paper performance before any live readiness discussion.

Current project rule: if any of these are missing, `final_recommendation` remains `NO LIVE`.
