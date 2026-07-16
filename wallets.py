"""Hot-wallet engine for crypto top-ups: BTC / LTC / ETH / SOL.

One BIP39 mnemonic (``HOT_WALLET_MNEMONIC``) derives a receiving address per
chain. Free public explorers/RPCs are used to watch for incoming payments,
query balances, and broadcast sweep (withdraw) transactions:

  BTC  mempool.space        LTC  litecoinspace.org
  ETH  publicnode RPC + blockscout        SOL  mainnet-beta RPC

Amounts are handled as ints in each chain's smallest unit (sats / wei /
lamports) to keep matching exact.
"""
import hashlib
import hmac as hmaclib
import logging
import time

from embit import bip32, bip39
from embit.networks import NETWORKS
from embit.script import address_to_scriptpubkey, p2pkh_from_p2wpkh, p2wpkh
from embit.transaction import Transaction, TransactionInput, TransactionOutput, Witness
from eth_account import Account
from solders.hash import Hash as SolHash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction as SolTransaction

log = logging.getLogger("reseller-bot.wallets")

Account.enable_unaudited_hdwallet_features()

LTC_NETWORK = dict(NETWORKS["main"], bech32="ltc", p2pkh=0x30, p2sh=0x32, wif=0xB0)

ETH_RPC = "https://ethereum-rpc.publicnode.com"
ETH_TXLIST = "https://eth.blockscout.com/api"
SOL_RPC = "https://api.mainnet-beta.solana.com"
COINGECKO = "https://api.coingecko.com/api/v3/simple/price"

SOL_TX_FEE = 5000  # lamports, single-signature transfer


def generate_mnemonic():
    import os as _os

    return bip39.mnemonic_from_bytes(_os.urandom(16))


def _slip10_ed25519(seed, path):
    key = hmaclib.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    k, c = key[:32], key[32:]
    for idx in path:
        data = b"\x00" + k + (idx | 0x80000000).to_bytes(4, "big")
        d = hmaclib.new(c, data, hashlib.sha512).digest()
        k, c = d[:32], d[32:]
    return k


class ChainError(Exception):
    pass


class BaseChain:
    code = ""
    name = ""
    coingecko_id = ""
    decimals = 0
    # Random dust added to invoice amounts so each is unique:
    # a value in dust_range multiplied by dust_unit (smallest units).
    dust_range = (1, 999)
    dust_unit = 1
    min_sweep = 0  # don't sweep below this (covers fees)

    def __init__(self, session, mnemonic):
        self.session = session
        self.seed = bip39.mnemonic_to_seed(mnemonic)
        self.address = self._derive()

    def _derive(self):
        raise NotImplementedError

    def format_amount(self, units):
        s = f"{units / 10 ** self.decimals:.{self.decimals}f}".rstrip("0").rstrip(".")
        return s or "0"

    def to_units(self, amount_float):
        return int(round(amount_float * 10 ** self.decimals))

    async def _get(self, url, **kw):
        async with self.session.get(url, timeout=20, **kw) as r:
            if r.status >= 400:
                raise ChainError(f"GET {url} -> {r.status}")
            ct = r.headers.get("Content-Type", "")
            return await (r.json() if "json" in ct else r.text())

    async def _rpc(self, url, method, params):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with self.session.post(url, json=payload, timeout=20) as r:
            data = await r.json()
        if "error" in data and data["error"]:
            raise ChainError(f"{method}: {data['error']}")
        return data["result"]

    async def incoming(self, since_ts):
        """Confirmed payments to our address since `since_ts`.

        Returns a list of (txid, amount_units) tuples.
        """
        raise NotImplementedError

    async def balance(self):
        raise NotImplementedError

    async def sweep(self, to_address):
        """Send the full spendable balance to `to_address`. Returns txid."""
        raise NotImplementedError

    def validate_address(self, addr):
        raise NotImplementedError


class _MempoolChain(BaseChain):
    """Shared logic for BTC + LTC (mempool.space-compatible APIs)."""

    api = ""
    network = None
    derivation = ""
    decimals = 8
    min_sweep = 10000  # sats

    def _derive(self):
        root = bip32.HDKey.from_seed(self.seed)
        self.key = root.derive(self.derivation)
        return p2wpkh(self.key).address(self.network)

    def validate_address(self, addr):
        try:
            address_to_scriptpubkey(addr)
        except Exception:  # noqa: BLE001
            return False
        prefix = self.network["bech32"] + "1"
        return addr.startswith(prefix) or addr[0] in "13LM"

    async def incoming(self, since_ts):
        txs = await self._get(f"{self.api}/api/address/{self.address}/txs")
        out = []
        for tx in txs:
            status = tx.get("status") or {}
            if not status.get("confirmed"):
                continue
            if status.get("block_time", 0) < since_ts:
                continue
            received = sum(
                v["value"]
                for v in tx.get("vout", [])
                if v.get("scriptpubkey_address") == self.address
            )
            if received > 0:
                out.append((tx["txid"], received))
        return out

    async def _utxos(self):
        utxos = await self._get(f"{self.api}/api/address/{self.address}/utxo")
        return [u for u in utxos if (u.get("status") or {}).get("confirmed")]

    async def balance(self):
        return sum(u["value"] for u in await self._utxos())

    async def fee_rate(self):
        fees = await self._get(f"{self.api}/api/v1/fees/recommended")
        return max(1, int(fees.get("halfHourFee", 2)))

    async def sweep(self, to_address):
        utxos = await self._utxos()
        total = sum(u["value"] for u in utxos)
        if not utxos or total < self.min_sweep:
            raise ChainError("balance too small to sweep")
        rate = await self.fee_rate()
        vsize = 11 + 68 * len(utxos) + 31
        fee = vsize * rate
        if total - fee <= 546:
            raise ChainError("balance would be dust after fees")
        ins = [
            TransactionInput(bytes.fromhex(u["txid"])[::-1], u["vout"])
            for u in utxos
        ]
        outs = [TransactionOutput(total - fee, address_to_scriptpubkey(to_address))]
        tx = Transaction(vin=ins, vout=outs)
        script_code = p2pkh_from_p2wpkh(p2wpkh(self.key))
        for i, u in enumerate(utxos):
            h = tx.sighash_segwit(i, script_code, u["value"])
            sig = self.key.sign(h)
            tx.vin[i].witness = Witness(
                [sig.serialize() + b"\x01", self.key.sec()]
            )
        async with self.session.post(
            f"{self.api}/api/tx", data=tx.serialize().hex(), timeout=20
        ) as r:
            body = await r.text()
            if r.status >= 400:
                raise ChainError(f"broadcast failed: {body}")
        return body.strip()


class BitcoinChain(_MempoolChain):
    code = "BTC"
    name = "Bitcoin"
    coingecko_id = "bitcoin"
    api = "https://mempool.space"
    network = NETWORKS["main"]
    derivation = "m/84h/0h/0h/0/0"


class LitecoinChain(_MempoolChain):
    code = "LTC"
    name = "Litecoin"
    coingecko_id = "litecoin"
    api = "https://litecoinspace.org"
    network = LTC_NETWORK
    derivation = "m/84h/2h/0h/0/0"
    min_sweep = 100000


class EthereumChain(BaseChain):
    code = "ETH"
    name = "Ethereum"
    coingecko_id = "ethereum"
    decimals = 18
    # ~1e12 wei of dust keeps displayed amounts short but unique.
    dust_range = (1, 999)
    dust_unit = 10 ** 12
    min_sweep = 10 ** 15  # 0.001 ETH

    def _derive(self):
        # eth_account handles BIP44 derivation from the mnemonic itself.
        self.acct = None
        return ""

    def __init__(self, session, mnemonic):
        self.session = session
        self.acct = Account.from_mnemonic(mnemonic, account_path="m/44'/60'/0'/0/0")
        self.address = self.acct.address

    def validate_address(self, addr):
        return isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42

    async def incoming(self, since_ts):
        data = await self._get(
            ETH_TXLIST,
            params={
                "module": "account",
                "action": "txlist",
                "address": self.address,
                "sort": "desc",
            },
        )
        result = data.get("result") if isinstance(data, dict) else None
        out = []
        for tx in result or []:
            if not isinstance(tx, dict):
                continue
            if (tx.get("to") or "").lower() != self.address.lower():
                continue
            if int(tx.get("timeStamp", 0)) < since_ts:
                continue
            if int(tx.get("confirmations", 0)) < 1 or tx.get("isError") == "1":
                continue
            value = int(tx.get("value", 0))
            if value > 0:
                out.append((tx["hash"], value))
        return out

    async def balance(self):
        res = await self._rpc(ETH_RPC, "eth_getBalance", [self.address, "latest"])
        return int(res, 16)

    async def sweep(self, to_address):
        bal = await self.balance()
        if bal < self.min_sweep:
            raise ChainError("balance too small to sweep")
        gas_price = int(await self._rpc(ETH_RPC, "eth_gasPrice", []), 16)
        gas_price = int(gas_price * 1.2)
        fee = 21000 * gas_price
        if bal <= fee:
            raise ChainError("balance would not cover gas")
        nonce = int(
            await self._rpc(ETH_RPC, "eth_getTransactionCount", [self.address, "latest"]),
            16,
        )
        tx = {
            "to": to_address,
            "value": bal - fee,
            "gas": 21000,
            "gasPrice": gas_price,
            "nonce": nonce,
            "chainId": 1,
        }
        signed = self.acct.sign_transaction(tx)
        return await self._rpc(
            ETH_RPC, "eth_sendRawTransaction", [signed.raw_transaction.to_0x_hex()]
        )


class SolanaChain(BaseChain):
    code = "SOL"
    name = "Solana"
    coingecko_id = "solana"
    decimals = 9
    dust_range = (1, 999)
    dust_unit = 10 ** 3
    min_sweep = 10 ** 7  # 0.01 SOL

    def _derive(self):
        self.keypair = Keypair.from_seed(_slip10_ed25519(self.seed, [44, 501, 0, 0]))
        return str(self.keypair.pubkey())

    def validate_address(self, addr):
        try:
            Pubkey.from_string(addr)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def incoming(self, since_ts):
        sigs = await self._rpc(
            SOL_RPC, "getSignaturesForAddress", [self.address, {"limit": 25}]
        )
        out = []
        for entry in sigs or []:
            if entry.get("err") is not None:
                continue
            if (entry.get("blockTime") or 0) < since_ts:
                continue
            tx = await self._rpc(
                SOL_RPC,
                "getTransaction",
                [
                    entry["signature"],
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
                ],
            )
            if not tx or tx.get("meta") is None:
                continue
            keys = [
                k["pubkey"] if isinstance(k, dict) else k
                for k in tx["transaction"]["message"]["accountKeys"]
            ]
            if self.address not in keys:
                continue
            idx = keys.index(self.address)
            delta = tx["meta"]["postBalances"][idx] - tx["meta"]["preBalances"][idx]
            if delta > 0:
                out.append((entry["signature"], delta))
        return out

    async def balance(self):
        res = await self._rpc(SOL_RPC, "getBalance", [self.address])
        return int(res["value"])

    async def sweep(self, to_address):
        bal = await self.balance()
        if bal < self.min_sweep:
            raise ChainError("balance too small to sweep")
        amount = bal - SOL_TX_FEE
        if amount <= 0:
            raise ChainError("balance would not cover the network fee")
        blockhash = await self._rpc(SOL_RPC, "getLatestBlockhash", [])
        recent = SolHash.from_string(blockhash["value"]["blockhash"])
        ix = transfer(
            TransferParams(
                from_pubkey=self.keypair.pubkey(),
                to_pubkey=Pubkey.from_string(to_address),
                lamports=amount,
            )
        )
        msg = Message([ix], self.keypair.pubkey())
        tx = SolTransaction([self.keypair], msg, recent)
        import base64

        raw = base64.b64encode(bytes(tx)).decode()
        return await self._rpc(
            SOL_RPC, "sendTransaction", [raw, {"encoding": "base64"}]
        )


CHAIN_CLASSES = [BitcoinChain, EthereumChain, SolanaChain, LitecoinChain]


class WalletManager:
    """Owns one chain handler per currency plus a cached USD price feed."""

    def __init__(self, session, mnemonic):
        self.chains = {cls.code: cls(session, mnemonic) for cls in CHAIN_CLASSES}
        self.session = session
        self._prices = {}
        self._prices_at = 0.0

    def chain(self, code):
        return self.chains[code]

    async def prices(self):
        """USD price per whole coin, cached for 2 minutes."""
        if self._prices and time.time() - self._prices_at < 120:
            return self._prices
        prices = await self._coingecko_prices()
        if prices is None:
            prices = await self._coinbase_prices()
        if not prices:
            if self._prices:
                return self._prices
            raise ChainError("price fetch failed (all sources)")
        self._prices, self._prices_at = prices, time.time()
        return prices

    async def _coingecko_prices(self):
        ids = ",".join(c.coingecko_id for c in self.chains.values())
        try:
            async with self.session.get(
                COINGECKO, params={"ids": ids, "vs_currencies": "usd"}, timeout=20
            ) as r:
                if r.status >= 400:
                    return None
                data = await r.json()
        except Exception:  # noqa: BLE001
            return None
        prices = {}
        for chain in self.chains.values():
            usd = (data.get(chain.coingecko_id) or {}).get("usd")
            if usd:
                prices[chain.code] = float(usd)
        return prices or None

    async def _coinbase_prices(self):
        prices = {}
        for code in self.chains:
            try:
                async with self.session.get(
                    f"https://api.coinbase.com/v2/prices/{code}-USD/spot", timeout=20
                ) as r:
                    if r.status >= 400:
                        continue
                    data = await r.json()
                prices[code] = float(data["data"]["amount"])
            except Exception:  # noqa: BLE001
                continue
        return prices or None
