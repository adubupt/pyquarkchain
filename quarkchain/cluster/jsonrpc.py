import asyncio
import inspect

from aiohttp import web
from decorator import decorator
from ethereum.utils import (
    is_numeric, is_string, int_to_big_endian, big_endian_to_int,
    encode_hex, decode_hex, sha3, zpad, denoms, int32)
from jsonrpcserver.aio import methods
from jsonrpcserver.exceptions import InvalidParams

from quarkchain.config import DEFAULT_ENV
from quarkchain.core import Address, Branch, Code, Transaction
from quarkchain.evm.transactions import Transaction as EvmTransaction
from quarkchain.utils import Logger


# defaults
default_startgas = 500 * 1000
default_gasprice = 60 * denoms.shannon


async def handle(request):
    request = await request.text()
    response = await methods.dispatch(request)
    if response.is_notification:
        return web.Response()
    else:
        return web.json_response(response, status=response.http_status)


def is_json_string(data):
    return isinstance(data, str)


def quantity_decoder(data):
    """Decode `data` representing a quantity."""
    # [NOTE]: decode to `str` for both python2 and python3
    if not is_json_string(data):
        success = False
    elif not data.startswith("0x"):
        success = False  # must start with 0x prefix
    elif len(data) > 3 and data[2] == "0":
        success = False  # must not have leading zeros (except `0x0`)
    else:
        data = data[2:]
        try:
            return int(data, 16)
        except ValueError:
            success = False
    assert not success
    raise InvalidParams("Invalid quantity encoding")


def quantity_encoder(i):
    """Encode integer quantity `data`."""
    assert is_numeric(i)
    data = int_to_big_endian(i)
    return str("0x" + (encode_hex(data).lstrip("0") or "0"))


def data_decoder(data):
    """Decode `data` representing unformatted data."""
    if not data.startswith("0x"):
        data = "0x" + data

    if len(data) % 2 != 0:
        # workaround for missing leading zeros from netstats
        assert len(data) < 64 + 2
        data = "0x" + "0" * (64 - (len(data) - 2)) + data[2:]

    try:
        return decode_hex(data[2:])
    except TypeError:
        raise InvalidParams("Invalid data hex encoding", data[2:])


def data_encoder(data, length=None):
    """Encode unformatted binary `data`.

    If `length` is given, the result will be padded like this: ``data_encoder("b\xff", 3) ==
    "0x0000ff"``.
    """
    s = encode_hex(data)
    if length is None:
        return str("0x" + s)
    else:
        return str("0x" + s.rjust(length * 2, "0"))


def obj_decoder(data, cls):
    raw = data_decoder(data)
    try:
        return cls.deserialize(raw)
    except Exception as e:
        raise InvalidParams(e)


def obj_encoder(obj, cls):
    assert isinstance(obj, cls)
    result = str("0x" + encode_hex(obj.serialize()))
    return result


def address_decoder(data):
    """Decode an address from hex with 0x prefix to Address."""
    return obj_decoder(data, Address)


def address_encoder(address):
    return obj_encoder(address, Address)


def branch_decoder(data):
    """Decode a branch from hex with 0x prefix to Branch."""
    obj_decoder(data, Branch)


def branch_encoder(branch):
    obj_encoder(branch, Branch)


def block_id_decoder(data):
    """Decode a block identifier as expected from :meth:`JSONRPCServer.get_block`."""
    if data in (None, "latest", "earliest", "pending"):
        return data
    else:
        return quantity_decoder(data)


def block_hash_decoder(data):
    """Decode a block hash."""
    decoded = data_decoder(data)
    if len(decoded) != 32:
        raise InvalidParams("Block hashes must be 32 bytes long")
    return decoded


def tx_hash_decoder(data):
    """Decode a transaction hash."""
    decoded = data_decoder(data)
    if len(decoded) != 32:
        raise InvalidParams("Transaction hashes must be 32 bytes long")
    return decoded


def bool_decoder(data):
    if not isinstance(data, bool):
        raise InvalidParams("Parameter must be boolean")
    return data


def decode_arg(name, decoder):
    """Create a decorator that applies `decoder` to argument `name`."""
    @decorator
    def new_f(f, *args, **kwargs):
        call_args = inspect.getcallargs(f, *args, **kwargs)
        call_args[name] = decoder(call_args[name])
        return f(**call_args)
    return new_f


def encode_res(encoder):
    """Create a decorator that applies `encoder` to the return value of the
    decorated function.
    """
    @decorator
    async def new_f(f, *args, **kwargs):
        res = await f(*args, **kwargs)
        return encoder(res)
    return new_f


class JSONRPCServer:

    def __init__(self, env, masterServer):
        app = web.Application()
        app.router.add_post("/", handle)
        self.runner = web.AppRunner(app)
        self.loop = asyncio.get_event_loop()
        self.port = env.config.LOCAL_SERVER_PORT
        self.env = env
        self.master = masterServer

        # Bind RPC handler functions to this instance
        for rpcName in methods:
            func = methods[rpcName]
            methods[rpcName] = func.__get__(self, self.__class__)

    def start(self):
        self.loop.run_until_complete(self.runner.setup())
        site = web.TCPSite(self.runner, "localhost", self.port)
        self.loop.run_until_complete(site.start())

    def shutdown(self):
        self.loop.run_until_complete(self.runner.cleanup())

    # JSON RPC handlers
    @methods.add
    @decode_arg("data", data_decoder)
    @encode_res(data_encoder)
    async def echo(self, data):
        return data

    @methods.add
    @decode_arg("address", address_decoder)
    @decode_arg("blockId", block_id_decoder)
    async def getTransactionCount(self, address, blockId="pending"):
        branch, count = await self.master.getTransactionCount(address)
        return {
            "branch": branch_encoder(branch),
            "count": quantity_encoder(count),
        }

    @methods.add
    async def sendTransaction(self, data):
        if not isinstance(data, dict):
            raise InvalidParams("Transaction must be an object")

        def getDataDefault(key, decoder, default=None):
            if key in data:
                return decoder(data[key])
            return default

        to = getDataDefault("to", address_decoder, None)
        gasKey = "gas" if "gas" in data else "startgas"
        startgas = getDataDefault(gasKey, quantity_decoder, default_startgas)
        gaspriceKey = "gasPrice" if "gasPrice" in data else "gasprice"
        gasprice = getDataDefault(gaspriceKey, quantity_decoder, default_gasprice)
        value = getDataDefault("value", quantity_decoder, 0)
        data_ = getDataDefault("data", data_decoder, b"")
        v = getDataDefault("v", quantity_decoder, 0)
        r = getDataDefault("r", quantity_decoder, 0)
        s = getDataDefault("s", quantity_decoder, 0)
        nonce = getDataDefault("nonce", quantity_decoder, None)

        branch = getDataDefault("branch", branch_decoder, None)
        withdraw = getDataDefault("withdraw", quantity_decoder, 0)
        withdrawTo = getDataDefault("withdrawTo", address_decoder, None)

        if nonce is None:
            raise InvalidParams("Missing nonce")
        if not (v and r and s):
            raise InvalidParams("Mising v, r, s")
        if branch is None:
            raise InvalidParams("Missing branch")
        if withdraw > 0 and withdrawTo is None:
            raise InvalidParams("Missing withdrawTo")

        evmTx = EvmTransaction(
            nonce, gasprice, startgas, to.recipient, value, data_, v, r, s,
            branchValue=branch.value,
            withdraw=withdraw,
            withdrawSign=1,
            withdrawTo=withdrawTo.serialize() if withdrawTo else b"",
        )
        tx = Transaction(code=Code.createEvmCode(evmTx))
        await self.master.addTx(tx, branch)
        Logger.debug("decoded tx", tx=tx.to_dict())
        return data_encoder(tx.hash)


if __name__ == "__main__":
    # web.run_app(app, port=5000)
    server = JSONRPCServer(DEFAULT_ENV, None)
    server.start()
    asyncio.get_event_loop().run_forever()