#!/usr/bin/env python3

import itertools as it, operator as op, functools as ft
import os, sys, stat, tempfile, pathlib, contextlib, logging, re
import math, base64, hashlib, json

from urllib.request import urlopen, Request, URLError, HTTPError

import cryptography # cryptography.io
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding, rsa, ec


acme_ca_shortcuts = dict(
	le='https://acme-v01.api.letsencrypt.org/directory',
	le_staging='https://acme-staging.api.letsencrypt.org/directory' )


class LogMessage:
	def __init__(self, fmt, a, k): self.fmt, self.a, self.k = fmt, a, k
	def __str__(self): return self.fmt.format(*self.a, **self.k) if self.a or self.k else self.fmt

class LogStyleAdapter(logging.LoggerAdapter):
	def __init__(self, logger, extra=None):
		super(LogStyleAdapter, self).__init__(logger, extra or {})
	def log(self, level, msg, *args, **kws):
		if not self.isEnabledFor(level): return
		log_kws = {} if 'exc_info' not in kws else dict(exc_info=kws.pop('exc_info'))
		msg, kws = self.process(msg, kws)
		self.logger._log(level, LogMessage(msg, args, kws), (), log_kws)

get_logger = lambda name: LogStyleAdapter(logging.getLogger(name))

@contextlib.contextmanager
def safe_replacement(path, *open_args, mode=None, **open_kws):
	path = str(path)
	if mode is None:
		try: mode = stat.S_IMODE(os.lstat(path).st_mode)
		except OSError: pass
	open_kws.update( delete=False,
		dir=os.path.dirname(path), prefix=os.path.basename(path)+'.' )
	if not open_args: open_kws['mode'] = 'w'
	with tempfile.NamedTemporaryFile(*open_args, **open_kws) as tmp:
		try:
			if mode is not None: os.fchmod(tmp.fileno(), mode)
			yield tmp
			if not tmp.closed: tmp.flush()
			os.rename(tmp.name, path)
		finally:
			try: os.unlink(tmp.name)
			except OSError: pass

def p(*a, file=None, end='\n', flush=False, **k):
	if len(a) > 0:
		fmt, a = a[0], a[1:]
		a, k = ( ([fmt.format(*a,**k)], dict())
			if isinstance(fmt, str) and (a or k)
			else ([fmt] + list(a), k) )
	print(*a, file=file, end=end, flush=flush, **k)

p_err = lambda *a,**k: p(*a, file=sys.stderr, **k) or 1


def b64_b2a_jose(data, uint_len=None):
	# https://jose.readthedocs.io/en/latest/
	if uint_len in [True, 'auto']:
		uint_len = divmod(math.log(data, 2), 8)
		uint_len = int(uint_len[0]) + 1 * (uint_len[1] != 0)
	if uint_len is not None:
		data = data.to_bytes(uint_len, 'big', signed=False)
		# print(':'.join('{:02x}'.format(b) for b in data))
	if isinstance(data, str): data = data.encode()
	return base64.urlsafe_b64encode(data).replace(b'=', b'').decode()


class AccKey:
	__slots__ = 't sk pk_hash jwk jws_alg sign_func'.split()
	def __init__(self, *args, **kws):
		for k,v in it.chain(zip(self.__slots__, args), kws.items()): setattr(self, k, v)
		self.jwk = self._jwk()
		self.jws_alg, self.sign_func = self._sign_func()
		self.pk_hash = self._pk_hash() # only used to id keys in this script

	def _jwk(self):
		# https://tools.ietf.org/html/rfc7517 + rfc7518
		pk_nums = self.sk.public_key().public_numbers()
		if self.t == 'rsa-4096':
			return dict( kty='RSA',
				n=b64_b2a_jose(pk_nums.n, True),
				e=b64_b2a_jose(pk_nums.e, True) )
		elif self.t == 'ec-384':
			return dict( kty='EC', crv='P-384',
				x=b64_b2a_jose(pk_nums.x, 48),
				y=b64_b2a_jose(pk_nums.y, 48) )
		else: raise ValueError(self.t)

	def _pk_hash(self, trunc_len=8):
		digest = hashes.Hash(hashes.SHA256(), default_backend())
		pk_jwa_str = '\0'.join('\0'.join(kv) for kv in sorted(
			(k, b64_b2a_jose(n, True) if isinstance(n ,int) else n)
			for k, n in self.jwk.items() ))
		digest.update('\0'.join([self.t, *pk_jwa_str]).encode())
		return b64_b2a_jose(digest.finalize())[:trunc_len]

	def _sign_func(self):
		# https://tools.ietf.org/html/rfc7518#section-3.1
		if self.t.startswith('rsa-'):
			# https://tools.ietf.org/html/rfc7518#section-3.1 mandates pkcs1.5
			alg, sign_func = 'RS256', ft.partial( self.sk.sign,
				padding=padding.PKCS1v15(), algorithm=hashes.SHA256() )
		elif self.t == 'ec-384':
			alg, sign_func = 'ES384', ft.partial(self._sign_func_es384, self.sk)
		else: raise ValueError(self.t)
		return alg, sign_func

	@staticmethod
	def _sign_func_es384(sk, data):
		# cryptography produces ASN.1 DER signature only,
		#  while ACME expects "r || s" values from there, so it have to be decoded.
		# Resulting DER struct: 0x30 b1 ( 0x02 b2 (vr) 0x02 b3 (vs) )
		#  where: b1 = length of stuff after it, b2 = len(vr), b3 = len(vs)
		#  vr and vs are encoded as signed ints, so can have extra leading 0x00
		sig_der = sk.sign(data, signature_algorithm=ec.ECDSA(hashes.SHA384()))
		rs_len, rn, r_len = sig_der[1], 4, sig_der[3]
		sn, s_len = rn + r_len + 2, sig_der[rn + r_len + 1]
		assert sig_der[0] == 0x30 and sig_der[rn-2] == sig_der[sn-2] == 0x02
		assert rs_len + 2 == len(sig_der) == r_len + s_len + 6
		r, s = sig_der[rn:rn+r_len].lstrip(b'\0'), sig_der[sn:sn+s_len].lstrip(b'\0')
		return r + s

	@classmethod
	def generate_to_file(cls, p_acc_key, key_type):
		acc_key = acc_key_t = None
		if key_type.startswith('rsa-'):
			if key_type != 'rsa-4096': return
			acc_key = rsa.generate_private_key(65537, 4096, default_backend())
		elif key_type.startswith('ec-'):
			if key_type != 'ec-384': return
			acc_key = ec.generate_private_key(ec.SECP384R1(), default_backend())
		if acc_key:
			acc_key_pem = acc_key.private_bytes(
				serialization.Encoding.PEM,
				serialization.PrivateFormat.PKCS8, serialization.NoEncryption() )
			p_acc_key.parent.mkdir(parents=True, exist_ok=True)
			with safe_replacement(p_acc_key, 'wb') as dst: dst.write(acc_key_pem)
			acc_key = cls(key_type, acc_key)
		return acc_key

	@classmethod
	def load_from_file(cls, p_acc_key):
		acc_key = serialization.load_pem_private_key(
			p_acc_key.read_bytes(), None, default_backend() )
		if isinstance(acc_key, rsa.RSAPrivateKey)\
			and acc_key.key_size == 4096: acc_key_t = 'rsa-4096'
		elif isinstance(acc_key, ec.EllipticCurvePrivateKey)\
			and acc_key.curve.name == 'secp384r1': acc_key_t = 'ec-384'
		else: return None
		return cls(acc_key_t, acc_key)


class AccMeta(dict):

	re_meta = re.compile(r'^\s*## acme\.(\S+?): (.*)?\s*$')

	@classmethod
	def load_from_key_file(cls, p_acc_key):
		self = cls()
		with p_acc_key.open() as src:
			for line in src:
				m = self.re_meta.search(line)
				if not m: continue
				k, v = m.groups()
				self[k] = json.loads(v)
		return self

	def save_to_key_file(self, p_acc_key):
		with safe_replacement(p_acc_key) as dst:
			with p_acc_key.open() as src:
				final_newline = True
				for line in src:
					m = self.re_meta.search(line)
					if m: continue
					dst.write(line)
					final_newline = line.endswith('\n')
			if not final_newline: dst.write('\n')
			for k, v in self.items():
				if v is None: continue
				dst.write('## acme.{}: {}\n'.format(k, json.dumps(v)))


class HTTPResponse:
	__slots__ = 'code reason headers body'.split()
	def __init__(self, *args, **kws):
		for k,v in it.chain( zip(self.__slots__, it.repeat(None)),
			zip(self.__slots__, args), kws.items() ): setattr(self, k, v)

def signed_req_body( acc_key, payload, kid=None,
		nonce=None, url=None, resource=None, encode=True ):
	# For all of the boulder-specific quirks implemented here, see:
	#  letsencrypt/boulder/blob/d26a54b/docs/acme-divergences.md
	kid = None # 2017-02-03: for letsencrypt/boulder, always requires jwk
	protected = dict(alg=acc_key.jws_alg, url=url)
	if not kid: protected['jwk'] = acc_key.jwk
	else: protected['kid'] = kid
	if nonce: protected['nonce'] = nonce
	if url: protected['url'] = url
	protected = b64_b2a_jose(json.dumps(protected))
	# 2017-02-03: "resource" is for letsencrypt/boulder
	if ( resource and isinstance(payload, dict)
		and 'resource' not in payload ): payload['resource'] = resource
	if not isinstance(payload, str):
		if not isinstance(payload, bytes): payload = json.dumps(payload)
		payload = b64_b2a_jose(payload)
	signature = b64_b2a_jose(
		acc_key.sign_func('{}.{}'.format(protected, payload).encode()) )
	body = dict(protected=protected, payload=payload, signature=signature)
	if encode: body = json.dumps(body).encode()
	return body

def signed_req( acc_key, url, payload, kid=None,
		nonce=None, resource=None, acme_url=None ):
	url_full = url if ':' in url else None
	if not url_full or not nonce: # XXX: use HEAD /acme/new-nonce
		assert acme_url, [url, acme_url] # need to query directory
		log.debug('Sending acme-directory http request to: {!r}', acme_url)
		with urlopen(acme_url) as r:
			assert r.getcode() == 200
			acme_dir = json.load(r)
			nonce = r.headers['Replay-Nonce']
		if not url_full: url_full = acme_dir[url]
		if not resource: resource = url
	body = signed_req_body( acc_key, payload,
		kid=kid, nonce=nonce, url=url_full, resource=resource )
	log.debug('Sending signed http request to URL: {!r} ...', url_full)
	req = Request( url_full, body,
		{ 'Content-Type': 'application/jose+json',
			'User-Agent': 'acme-cert-tool/1.0 (+https://github.com/mk-fg/acme-cert-tool)' } )
	try:
		try: r = urlopen(req)
		except HTTPError as err: r = err
		res = HTTPResponse(r.getcode(), r.reason, r.headers, r.read())
		r.close()
	except URLError as r: res = HTTPResponse(reason=r.reason)
	log.debug('... http reponse: {} {}', res.code or '-', res.reason)
	return res


def main(args=None):
	import argparse, textwrap

	class SmartHelpFormatter(argparse.HelpFormatter):
		def _fill_text(self, text, width, indent):
			return super(SmartHelpFormatter, self)._fill_text(text, width, indent)\
				if '\n' not in text else ''.join(indent + line for line in text.splitlines(keepends=True))
		def _split_lines(self, text, width):
			return super(SmartHelpFormatter, self)._split_lines(text, width)\
				if '\n' not in text else text.splitlines()

	parser = argparse.ArgumentParser(
		formatter_class=SmartHelpFormatter,
		description='Lets Encrypt CA interaction tool to make'
			' it authorize domain and sign/renew/revoke TLS certs.')
	# XXX: add usage examples maybe

	group = parser.add_argument_group('ACME authentication')
	group.add_argument('-k', '--account-key',
		metavar='path', required=True, help=textwrap.dedent('''\
			Path to ACME domain-specific private key to use (pem with pkcs8/openssl/pkcs1).
			All operations wrt current domain will be authenticated using this key.
			It has nothing to do with actual issued TLS certs and cannot be reused in them.
			Has no default value on purpose, must be explicitly specified.
			If registered with ACME server, account URL will also be stored in the file alongside key.
			If --gen-key (or -g/--gen-key-if-missing) is also specified,
			 will be generated and path (incl. directories) will be created.'''))
	group.add_argument('-d', '--acme-dir',
		metavar='path', default='.', help=textwrap.dedent('''\
			Directory that is served by domain\'s httpd at "/.well-known/acme-challenge/".
			Will be created, if does not exist already. Default: current directory.'''))
	group.add_argument('-s', '--acme-service',
		metavar='url-or-name', default='le-staging', help=textwrap.dedent('''\
			ACME directory URL (or shortcut) of Cert Authority (CA) service to interact with.
			Available shortcuts: le - Let\'s Encrypt, le-staging - Let\'s Encrypt staging server.
			Default: %(default)s'''))

	group = parser.add_argument_group('Domain-specific key (--account-key) generation',
		description='Generated keys are always stored in pem/pkcs8 format with no encryption.')
	group.add_argument('-g', '--gen-key-if-missing', action='store_true',
		help='Generate ACME authentication key before operation, if it does not exist already.')
	group.add_argument('--gen-key', action='store_true',
		help='Generate new ACME authentication key regardless of whether --account-key path exists.')
	group.add_argument('-t', '--key-type',
		metavar='type', choices=['rsa-4096', 'ec-384'], default='ec-384',
		help='ACME authentication key type to generate.'
			' Possible values: rsa-4096, ec-384 (secp384r1). Default: %(default)s')

	group = parser.add_argument_group('Account/key registration and update options')
	group.add_argument('-r', '--register', action='store_true',
		help='Register key with CA before verifying domains. Must be done at least once for key.'
			' Should be safe to try doing that more than once,'
				' CA will just return "409 Conflict" error (ignored by this script).'
			' Performed automatically if --account-key file does not have account URL stored there.')
	group.add_argument('-e', '--contact-email', metavar='email',
		help='Email address for any account-specific issues,'
				' warnings and notifications to register along with the key.'
			' If was not specified previously or differs from that, will be automatically updated.')
	group.add_argument('-o', '--account-key-old', metavar='path',
		help='Issue a key-change command from an old key specified with this option.'
			' Overrides -r/--register option - if old key is specified,'
				' new one (specified as --account-key) will attached to same account as the old one.')

	group = parser.add_argument_group('Misc other options')
	group.add_argument('-u', '--umask', metavar='octal', default='0077',
		help='Umask to set before creating anything.'
			' Default is 0077 to create 0600/0700 (user-only access) files/dirs.'
			' Special value "-" (dash) will make script leave umask unchanged.')
	group.add_argument('--debug', action='store_true', help='Verbose operation mode.')


	cmds = parser.add_subparsers(title='Commands', dest='call')

	cmd = cmds.add_parser('account-info',
		help='Request and print info for ACME account associated with the specified key.')
	cmd = cmds.add_parser('account-deactivate',
		help='Deactivate (block/remove) ACME account'
			' associated with the key. It cannot be reactivated again.')

	cmd = cmds.add_parser('verify-domain',
		help='Verify (and optionally register) --account-key for specified domain(s).')
	cmd.add_argument('domain', nargs='+',
		help='Domain(s) to authenticate with specified key (--account-key).')


	opts = parser.parse_args(sys.argv[1:] if args is None else args)

	global log
	logging.basicConfig( datefmt='%Y-%m-%d %H:%M:%S',
		format='%(asctime)s :: %(name)s %(levelname)s :: %(message)s',
		level=logging.DEBUG if opts.debug else logging.WARNING )
	log = get_logger('main')


	if opts.umask != '-': os.umask(int(opts.umask, 8))

	p_acme_dir = pathlib.Path(opts.acme_dir)
	p_acme_dir.mkdir(parents=True, exist_ok=True)

	acme_url = opts.acme_service
	if ':' not in acme_url:
		try: acme_url = acme_ca_shortcuts[acme_url.replace('-', '_')]
		except KeyError: parser.error('Unkown --acme-service shortcut: {!r}'.format(acme_url))

	p_acc_key = pathlib.Path(opts.account_key)
	if opts.gen_key or (opts.gen_key_if_missing and not p_acc_key.exists()):
		acc_key = AccKey.generate_to_file(p_acc_key, opts.key_type)
		if not acc_key:
			parser.error('Unknown/unsupported --key-type type value: {!r}'.format(opts.key_type))
	elif p_acc_key.exists():
		acc_key = AccKey.load_from_file(p_acc_key)
		if not acc_key: parser.error('Unknown/unsupported key type: {}'.format(p_acc_key))
	else: parser.error('Specified --account-key path does not exists: {!r}'.format(p_acc_key))
	acc_meta = AccMeta.load_from_key_file(p_acc_key)
	log.debug( 'Using {} domain key: {} (acme acc url: {})',
		acc_key.t, acc_key.pk_hash, acc_meta.get('acc.url') )

	print_req_err_info = lambda: p_err(
		'Server response: {} {}{}{}', res.code or '-', res.reason or '-', '\n' if res.body else '',
		''.join('  {}'.format(line) for line in res.body.decode().splitlines(keepends=True)) )


	### Handle account status

	acc_key_old = opts.account_key_old
	acc_register = opts.register or acc_key_old or not acc_meta.get('acc.url')
	acc_contact = opts.contact_email and 'mailto:{}'.format(opts.contact_email)

	if acc_register:
		payload_reg = {'terms-of-service-agreed': True}

		if not os.access(p_acc_key, os.W_OK):
			return p_err( 'ERROR: Account registration required,'
				' but key file is not writable (to store new-reg url there): {}', p_acc_key )
		if acc_meta.get('acc.url'):
			log.warning( 'Specified --account-key already marked as'
				' registered (url: {}), proceeding regardless.', acc_meta['acc.url'] )
		if acc_key_old:
			if opts.register:
				log.debug( 'Both -r/--register and'
					' -o/--account-key-old are specified, acting according to latter option.' )
			p_acc_key_old = pathlib.Path(acc_key_old)
			acc_key_old = AccKey.load_from_file(p_acc_key_old)
			if not acc_key_old:
				parser.error('Unknown/unsupported key type'
					' specified with -o/--account-key-old: {}'.format(p_acc_key))
			acc_meta_old = AccMeta.load_from_key_file(p_acc_key_old)
			acc_url_old = acc_meta_old.get('acc.url')
			if not acc_url_old:
				log.debug( 'Old key file (-o/--account-key-old) does'
					' not have registration URL, will be fetched via new-reg request' )
				res = signed_req(acc_key_old, 'new-reg', payload_reg, acme_url=acme_url)
				if res.code not in [201, 409]:
					p_err('ERROR: ACME new-reg request for old key (-o/--account-key-old) failed')
					return print_req_err_info()
				acc_url_old = res.headers['Location']

		if not acc_key_old: # new-reg
			if acc_contact: payload_reg['contact'] = [acc_contact]
			res = signed_req(acc_key, 'new-reg', payload_reg, acme_url=acme_url)
			if res.code not in [201, 409]:
				p_err('ERROR: ACME new-reg (key registration) request failed')
				return print_req_err_info()
			log.debug('Account registration status: {} {}', res.code, res.reason)
			acc_meta['acc.url'] = res.headers['Location']
			if res.code == 201: acc_meta['acc.contact'] = acc_contact
		else: # key-change
			with urlopen(acme_url) as r: # need same URL for both inner and outer payloads
				assert r.getcode() == 200
				resource, acme_dir = 'key-change', json.load(r)
				url, nonce = acme_dir[resource], r.headers['Replay-Nonce']
			payload = dict(account=acc_url_old, newKey=acc_key.jwk)
			payload = signed_req_body(acc_key, payload, url=url, encode=False)
			# According to https://tools.ietf.org/html/draft-ietf-acme-acme-04#section-5.2 ,
			#  only new-reg and revoke-cert should have jwk instead of kid,
			#  but 6.3.2 explicitly mentions jwks, so guess it should also be exception here.
			res = signed_req(acc_key_old, url, payload, nonce=nonce, resource=resource)
			if res.code not in [200, 201, 202]:
				p_err('ERROR: ACME key-change request failed')
				return print_req_err_info()
			log.debug('Account key-change success: {} -> {}', acc_key_old.pk_hash, acc_key.pk_hash)
			acc_meta['acc.url'] = acc_url_old
			acc_meta['acc.contact'] = acc_meta_old.get('acc.contact')
		acc_meta.save_to_key_file(p_acc_key)

	if acc_contact and acc_contact != acc_meta.get('acc.contact'):
		res = signed_req( acc_key, acc_meta['acc.url'],
			dict(resource='reg', contact=[acc_contact]),
			kid=acc_meta['acc.url'], acme_url=acme_url )
		if res.code not in [200, 201, 202]:
			p_err('ERROR: ACME contact info update request failed')
			return print_req_err_info()
		log.debug('Account contact info updated: {!r} -> {!r}', acc_meta['acc.contact'], acc_contact)
		acc_meta['acc.contact'] = acc_contact
		acc_meta.save_to_key_file(p_acc_key)

	acme_url_req = ft.partial( signed_req,
		acc_key, acme_url=acme_url, kid=acc_meta['acc.url'] )


	### Handle commands

	if opts.call == 'account-info':
		res = acme_url_req(acc_meta['acc.url'], dict(resource='reg'))
		if res.code not in [200, 201, 202]:
			p_err('ERROR: ACME account info request failed')
			return print_req_err_info()
		p(res.body.decode())

	elif opts.call == 'account-deactivate':
		res = acme_url_req(acc_meta['acc.url'], dict(resource='reg', status='deactivated'))
		if res.code != 200:
			p_err('ERROR: ACME account deactivation request failed')
			return print_req_err_info()
		p(res.body.decode())

	elif opts.call == 'verify-domain': pass
		# for domain in opts.domain:
		# 	log.info('Verifying domain: {!r}', domain)
		# 	res = acme_url_req('new-authz', dict(identifier=dict(type='dns', value=domain)))
		# 	if res.code != 201:
		# 		p_err('ERROR: ACME new-authz request failed for domain: {!r}', domain)
		# 		return print_req_err_info()
		# 	res.body

	elif not opts.call: parser.error('No command specified')
	else: parser.error('Unrecognized command: {!r}'.format(opts.call))


if __name__ == '__main__': sys.exit(main())
