//! lighter-cli — thin JSON wrapper over lighter-sdk for the carry bot.
//!
//! Every command prints exactly ONE compact JSON object to stdout. All logs go
//! to stderr so stdout stays machine-parseable. Non-zero exit on failure, with
//! {"ok":false,"error":"..."} on stdout.
//!
//! Commands:
//!   account
//!   market_info <symbol>
//!   position <symbol>
//!   market <buy|sell> <symbol> <qty> [--reduce] [--slip <pct>]
//!   limit  <buy|sell> <symbol> <qty> <price> [--reduce]
//!   close  <symbol>
//!   cancel <symbol> <order_index>
//!   leverage <symbol> <lev>
//!
//! Env: LIGHTER_API_KEY_PRIVATE, LIGHTER_API_KEY_INDEX, LIGHTER_ACCOUNT_INDEX,
//!      LIGHTER_MAINNET (default true)

use anyhow::{anyhow, Result};
use lighter_sdk::{LighterClient, LighterSigner, MarketInfo};
use rust_decimal::Decimal;
use serde_json::json;
use std::env;
use std::str::FromStr;

fn align_price(price: Decimal, m: &MarketInfo) -> Decimal {
    let scale = Decimal::from(10_i64.pow(m.supported_price_decimals));
    (price * scale).floor() / scale
}
fn align_qty(q: Decimal, m: &MarketInfo) -> Decimal {
    let scale = Decimal::from(10_i64.pow(m.supported_size_decimals));
    (q * scale).floor() / scale
}

fn out(v: serde_json::Value) {
    println!("{}", v);
}

#[tokio::main]
async fn main() {
    dotenv::dotenv().ok();
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr) // keep stdout pure JSON
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("warn")),
        )
        .init();

    match run().await {
        Ok(v) => out(v),
        Err(e) => {
            out(json!({"ok": false, "error": e.to_string()}));
            std::process::exit(1);
        }
    }
}

async fn run() -> Result<serde_json::Value> {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        return Err(anyhow!("no command"));
    }
    let cmd = args[1].as_str();

    let api_key_private = env::var("LIGHTER_API_KEY_PRIVATE")
        .map_err(|_| anyhow!("LIGHTER_API_KEY_PRIVATE not set"))?;
    let api_key_index: u8 = env::var("LIGHTER_API_KEY_INDEX")
        .unwrap_or_else(|_| "0".into())
        .parse()
        .map_err(|_| anyhow!("bad LIGHTER_API_KEY_INDEX"))?;
    let account_index: i64 = env::var("LIGHTER_ACCOUNT_INDEX")
        .map_err(|_| anyhow!("LIGHTER_ACCOUNT_INDEX not set"))?
        .parse()
        .map_err(|_| anyhow!("bad LIGHTER_ACCOUNT_INDEX"))?;
    let is_mainnet = env::var("LIGHTER_MAINNET")
        .map(|v| v == "true" || v == "1")
        .unwrap_or(true);

    let signer = LighterSigner::new(&api_key_private, api_key_index, account_index, is_mainnet)?;
    let client = if is_mainnet {
        LighterClient::mainnet(signer)
    } else {
        LighterClient::testnet(signer)
    };
    client.sync_nonce().await?;

    match cmd {
        "account" => {
            let a = client.get_account_info().await?;
            let positions: Vec<_> = a
                .positions
                .iter()
                .filter(|p| p.position.parse::<f64>().unwrap_or(0.0) != 0.0)
                .map(|p| {
                    json!({
                        "symbol": p.symbol.clone().or(p.market_symbol.clone()),
                        "market_id": p.market_id,
                        "size": p.position,
                        "sign": p.sign,
                        "entry": p.avg_entry_price,
                        "upnl": p.unrealized_pnl,
                    })
                })
                .collect();
            Ok(json!({
                "ok": true,
                "index": a.index,
                "l1_address": a.l1_address,
                "collateral": a.collateral,
                "available_balance": a.available_balance,
                "total_asset_value": a.total_asset_value,
                "positions": positions,
            }))
        }

        "market_info" => {
            let sym = args.get(2).ok_or_else(|| anyhow!("market_info <symbol>"))?;
            let m = client.get_market_info(sym).await?;
            let (bid, ask) = client.get_best_prices(sym).await?;
            Ok(json!({
                "ok": true,
                "market_id": m.market_id,
                "symbol": m.symbol,
                "price_decimals": m.supported_price_decimals,
                "size_decimals": m.supported_size_decimals,
                "min_base": m.min_base_amount,
                "min_quote": m.min_quote_amount,
                "best_bid": bid.to_string(),
                "best_ask": ask.to_string(),
            }))
        }

        "position" => {
            let sym = args.get(2).ok_or_else(|| anyhow!("position <symbol>"))?;
            match client.get_position(sym).await? {
                Some((size, entry)) => Ok(json!({
                    "ok": true, "symbol": sym,
                    "size": size.to_string(), "entry": entry.to_string(),
                    "flat": size == Decimal::ZERO,
                })),
                None => Ok(json!({"ok": true, "symbol": sym, "flat": true})),
            }
        }

        "market" => {
            // market <buy|sell> <symbol> <qty> [--reduce] [--slip <pct>]
            if args.len() < 5 {
                return Err(anyhow!("market <buy|sell> <symbol> <qty> [--reduce] [--slip pct]"));
            }
            let is_buy = args[2] == "buy";
            let sym = &args[3];
            let qty: Decimal = args[4].parse()?;
            let reduce = args.iter().any(|a| a == "--reduce");
            let slip_pct: Decimal = arg_val(&args, "--slip")
                .map(|s| Decimal::from_str(&s))
                .transpose()?
                .unwrap_or_else(|| Decimal::from_str("2").unwrap()); // default 2% ceiling

            let m = client.get_market_info(sym).await?;
            let (bid, ask) = client.get_best_prices(sym).await?;
            let factor = slip_pct / Decimal::from(100);
            let raw = if is_buy {
                ask * (Decimal::ONE + factor)
            } else {
                bid * (Decimal::ONE - factor)
            };
            let slip_price = align_price(raw, &m);
            let aq = align_qty(qty, &m);
            let r = client
                .place_market_order(sym, aq, is_buy, reduce, slip_price)
                .await?;
            Ok(json!({
                "ok": true, "kind": "market", "symbol": sym,
                "side": if is_buy {"buy"} else {"sell"},
                "qty": aq.to_string(), "slip_price": slip_price.to_string(),
                "best_bid": bid.to_string(), "best_ask": ask.to_string(),
                "order_id": r.order_id, "tx_hash": r.tx_hash, "status": r.status,
            }))
        }

        "limit" => {
            // limit <buy|sell> <symbol> <qty> <price> [--reduce]
            if args.len() < 6 {
                return Err(anyhow!("limit <buy|sell> <symbol> <qty> <price> [--reduce]"));
            }
            let is_buy = args[2] == "buy";
            let sym = &args[3];
            let qty: Decimal = args[4].parse()?;
            let price: Decimal = args[5].parse()?;
            let reduce = args.iter().any(|a| a == "--reduce");
            let m = client.get_market_info(sym).await?;
            let ap = align_price(price, &m);
            let aq = align_qty(qty, &m);
            let r = client.place_limit_order(sym, ap, aq, is_buy, reduce).await?;
            Ok(json!({
                "ok": true, "kind": "limit", "symbol": sym,
                "side": if is_buy {"buy"} else {"sell"},
                "qty": aq.to_string(), "price": ap.to_string(),
                "order_id": r.order_id, "tx_hash": r.tx_hash, "status": r.status,
            }))
        }

        "close" => {
            let sym = args.get(2).ok_or_else(|| anyhow!("close <symbol>"))?;
            match client.get_position(sym).await? {
                Some((size, _)) if size != Decimal::ZERO => {
                    client.close_position(sym).await?;
                    Ok(json!({"ok": true, "closed": true, "symbol": sym, "was": size.to_string()}))
                }
                _ => Ok(json!({"ok": true, "closed": false, "symbol": sym, "reason": "flat"})),
            }
        }

        "cancel" => {
            if args.len() < 4 {
                return Err(anyhow!("cancel <symbol> <order_index>"));
            }
            let sym = &args[2];
            let oi: i64 = args[3].parse()?;
            client.cancel_order(sym, oi).await?;
            Ok(json!({"ok": true, "cancelled": true, "symbol": sym, "order_index": oi}))
        }

        "leverage" => {
            if args.len() < 4 {
                return Err(anyhow!("leverage <symbol> <lev>"));
            }
            let sym = &args[2];
            let lev: u32 = args[3].parse()?;
            client.set_leverage(sym, lev, true).await?; // cross margin
            Ok(json!({"ok": true, "symbol": sym, "leverage": lev}))
        }

        other => Err(anyhow!(format!("unknown command: {}", other))),
    }
}

fn arg_val(args: &[String], flag: &str) -> Option<String> {
    args.iter().position(|a| a == flag).and_then(|i| args.get(i + 1).cloned())
}
