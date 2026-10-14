# Windows certificate fixtures

These DER files are public, self-signed test certificates used by the native
Windows trust-store tests. They contain no private keys:

| File | EKU and validity |
| --- | --- |
| `server-auth.der` | `serverAuth`; 2026-10-14 12:27:41Z through 2036-10-11 12:27:41Z |
| `code-signing.der` | `codeSigning`; 2026-10-14 12:27:44Z through 2036-10-11 12:27:44Z |
| `all-uses.der` | no EKU restriction; 2026-10-14 12:27:46Z through 2036-10-11 12:27:46Z |
| `expired-server-auth.der` | `serverAuth`; 2020-01-01 00:00:00Z through 2021-01-01 00:00:00Z |

The first three were generated on 2026-10-14 in a task-only temporary
directory with LibreSSL/OpenSSL. The `server-auth.der` command was:

```sh
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout server-auth.key -out server-auth.pem -days 3650 \
  -subj "/CN=rex-server-auth-fixture" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "extendedKeyUsage=serverAuth"
openssl x509 -in server-auth.pem -outform DER -out server-auth.der
```

`code-signing.der` used the same command with `server-auth` replaced by
`code-signing`, the subject `rex-code-signing-fixture`, and
`extendedKeyUsage=codeSigning`. `all-uses.der` used the same command with
`all-uses`, the subject `rex-all-uses-fixture`, and the extended-key-usage
line omitted.

For `expired-server-auth.der`, a temporary OpenSSL CA database signed a fresh
request with the fixed validity interval:

```sh
openssl req -new -newkey rsa:2048 -nodes \
  -keyout expired.key -out expired.csr -subj "/CN=rex-expired-fixture"
openssl ca -config expired.cnf -batch -selfsign -keyfile expired.key \
  -in expired.csr -startdate 20200101000000Z -enddate 20210101000000Z \
  -out expired.pem
openssl x509 -in expired.pem -outform DER -out expired-server-auth.der
```

The temporary directory contained the generated keys, CSRs, PEM files and CA
database; it was not copied into the repository. The tracked inputs are only
the four DER certificates above.
