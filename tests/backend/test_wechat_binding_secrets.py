"""Bounded split storage; tests never use native Credential Manager."""
import json
import pytest
from app.secret_store import InMemorySecretStore, InvalidSecretValueError, SecretStoreOSError
from app import wechat_binding_secrets as bundle


@pytest.mark.parametrize('value',['synthetic-token','x'*16384,'中'*16384,'🙂'*8192,
                                'a'*1023+'🙂'+'b'*(16384-1025)],
                         ids=['short','ascii-max','bmp-max','surrogate-pair-max','mixed-boundary-max'])
def test_round_trip_keeps_every_native_entry_within_existing_limit(value):
    store=InMemorySecretStore()
    ref=bundle.put_secret(store,value,'context')
    assert len(ref)<=64 and bundle.get_secret(store,ref)==value
    assert all(len(v.encode('utf-16-le'))<=2048 for v in store._values.values())
    bundle.delete_secret(store,ref)
    assert store._values=={}


@pytest.mark.parametrize('value',['', 'x'*16385, '\x00', '\ud800'],ids=['empty','oversize','null','invalid-unicode'])
def test_invalid_values_do_not_create_partial_credentials(value):
    store=InMemorySecretStore()
    with pytest.raises(InvalidSecretValueError):bundle.put_secret(store,value,'bot')
    assert store._values=={}


def test_legacy_opaque_references_still_read_and_delete():
    store=InMemorySecretStore();store.put('wechat-bot-old','synthetic-legacy')
    assert bundle.get_secret(store,'wechat-bot-old')=='synthetic-legacy'
    bundle.delete_secret(store,'wechat-bot-old')
    assert store._values=={}


def test_write_failure_rolls_back_manifest_and_all_chunks_without_echo():
    class FailStore(InMemorySecretStore):
        def put(self,ref,value):
            super().put(ref,value)
            if ref.endswith('-c01'):raise SecretStoreOSError()
    store=FailStore()
    with pytest.raises(SecretStoreOSError) as exc:bundle.put_secret(store,'private-'*400,'cursor')
    assert store._values=={} and 'private-' not in str(exc.value)


def test_corrupt_manifest_cannot_read_or_delete_arbitrary_credential():
    store=InMemorySecretStore();store.put('unrelated-credential','keep-me')
    ref=bundle.put_secret(store,'synthetic','bot')
    store._values[ref]=json.dumps({'v':1,'refs':['unrelated-credential']})
    with pytest.raises(SecretStoreOSError):bundle.get_secret(store,ref)
    with pytest.raises(SecretStoreOSError):bundle.delete_secret(store,ref)
    assert store.get('unrelated-credential')=='keep-me'


def test_tampered_or_missing_chunk_fails_closed():
    store=InMemorySecretStore();ref=bundle.put_secret(store,'a'*2000,'context')
    store._values[ref+'-c00']='b'*1024
    with pytest.raises(SecretStoreOSError):bundle.get_secret(store,ref)
    del store._values[ref+'-c00']
    with pytest.raises(SecretStoreOSError):bundle.get_secret(store,ref)


def test_failed_delete_retains_manifest_for_exact_retry():
    class FailStore(InMemorySecretStore):
        fail=False
        def delete(self,ref):
            if self.fail and ref.endswith('-c00'):raise SecretStoreOSError()
            super().delete(ref)
    store=FailStore();ref=bundle.put_secret(store,'x'*2100,'cursor');store.fail=True
    with pytest.raises(SecretStoreOSError):bundle.delete_secret(store,ref)
    assert ref in store._values
    store.fail=False;bundle.delete_secret(store,ref)
    assert store._values=={}


def test_manifest_read_is_strict_and_bounded():
    store=InMemorySecretStore();ref=bundle.put_secret(store,'synthetic','bot')
    for invalid in [{'v':True,'n':1,'bytes':18,'sha256':'0'*64},
                    {'v':1,'n':18,'bytes':18,'sha256':'0'*64},
                    {'v':1,'n':1,'bytes':True,'sha256':'0'*64}]:
        store._values[ref]=json.dumps(invalid)
        with pytest.raises(SecretStoreOSError):bundle.get_secret(store,ref)
