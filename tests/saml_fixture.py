"""IdP SAML finto: legge l'AuthnRequest dal redirect di wikibridge e
costruisce una Response con asserzione firmata (RSA-SHA256) da postare
sull'ACS. Le varianti (non firmata, altra chiave, manomessa, audience o
scadenze sbagliate) servono a provare che vengono rifiutate."""
from __future__ import annotations

import base64
import datetime as dt
import uuid
import zlib
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

IDP_ENTITY = "https://idp.example/saml"
IDP_SSO = "https://idp.example/sso"
GROUPS = "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"
OID = "http://schemas.microsoft.com/identity/claims/objectidentifier"


def _keypair(cn: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30))
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return key_pem, cert.public_bytes(serialization.Encoding.PEM).decode()


def _ts(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeSamlIdP:
    def __init__(self):
        self.key, self.cert = _keypair("idp")
        self.other_key, self.other_cert = _keypair("attacker")

    @staticmethod
    def request_of(location: str) -> tuple[str, str]:
        """Dal redirect verso l'IdP: (ID dell'AuthnRequest, RelayState)."""
        q = parse_qs(urlparse(location).query)
        xml = zlib.decompress(base64.b64decode(q["SAMLRequest"][0]), -15).decode()
        req_id = xml.split('ID="', 1)[1].split('"', 1)[0]
        return req_id, q["RelayState"][0]

    def response(self, *, in_response_to: str, acs: str, audience: str,
                 nameid: str, attrs: dict | None = None,
                 nameid_format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
                 sign: str = "good", tamper: bool = False,
                 expired: bool = False) -> str:
        """SAMLResponse in base64, pronta per il POST sull'ACS."""
        from onelogin.saml2.utils import OneLogin_Saml2_Utils
        now = dt.datetime.now(dt.timezone.utc)
        if expired:
            now -= dt.timedelta(hours=2)
        until = now + dt.timedelta(minutes=5)
        values = "".join(
            f'<saml:Attribute Name="{escape(k)}">' + "".join(
                f"<saml:AttributeValue>{escape(v)}</saml:AttributeValue>"
                for v in (vs if isinstance(vs, list) else [vs]))
            + "</saml:Attribute>" for k, vs in (attrs or {}).items())
        aid = f"_a{uuid.uuid4().hex}"
        assertion = (
            f'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
            f'ID="{aid}" Version="2.0" IssueInstant="{_ts(now)}">'
            f"<saml:Issuer>{IDP_ENTITY}</saml:Issuer>"
            f'<saml:Subject><saml:NameID Format="{nameid_format}">{escape(nameid)}</saml:NameID>'
            f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
            f'<saml:SubjectConfirmationData InResponseTo="{in_response_to}" '
            f'NotOnOrAfter="{_ts(until)}" Recipient="{acs}"/></saml:SubjectConfirmation>'
            f"</saml:Subject>"
            f'<saml:Conditions NotBefore="{_ts(now - dt.timedelta(minutes=1))}" '
            f'NotOnOrAfter="{_ts(until)}"><saml:AudienceRestriction>'
            f"<saml:Audience>{escape(audience)}</saml:Audience>"
            f"</saml:AudienceRestriction></saml:Conditions>"
            f'<saml:AuthnStatement AuthnInstant="{_ts(now)}" SessionIndex="_s1">'
            f"<saml:AuthnContext><saml:AuthnContextClassRef>"
            f"urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
            f"</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
            + (f"<saml:AttributeStatement>{values}</saml:AttributeStatement>" if values else "")
            + "</saml:Assertion>")
        if sign != "none":
            key, cert = ((self.key, self.cert) if sign == "good"
                         else (self.other_key, self.other_cert))
            assertion = OneLogin_Saml2_Utils.add_sign(assertion, key, cert)
            if isinstance(assertion, bytes):
                assertion = assertion.decode()
        if tamper:                        # dopo la firma: deve essere scoperto
            assertion = assertion.replace("grp-utenti", "grp-admins")
        # la dichiarazione XML dell'asserzione firmata non può stare dentro
        if assertion.startswith("<?xml"):
            assertion = assertion.split("?>", 1)[1]
        response = (
            f'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            f'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_r{uuid.uuid4().hex}" '
            f'Version="2.0" IssueInstant="{_ts(now)}" Destination="{acs}" '
            f'InResponseTo="{in_response_to}">'
            f"<saml:Issuer>{IDP_ENTITY}</saml:Issuer>"
            f'<samlp:Status><samlp:StatusCode '
            f'Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
            f"{assertion}</samlp:Response>")
        return base64.b64encode(response.encode()).decode()


def configure(cfg, idp: FakeSamlIdP, *, roles_source="assertion"):
    cfg.identity_provider = "saml"
    cfg.saml_idp_entity_id = IDP_ENTITY
    cfg.saml_idp_sso_url = IDP_SSO
    cfg.saml_idp_cert = idp.cert
    cfg.saml_roles_source = roles_source
    if roles_source == "assertion":
        cfg.saml_user_id_attr = OID
        cfg.identity_role_map = {"grp-legal": "team-legal", "grp-admins": "admin"}
