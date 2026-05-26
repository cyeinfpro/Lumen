use anyhow::{Context, Result};
use keyring::Entry;

const SERVICE: &str = "com.lumen.desktop";

fn set_secret(kind: &str, name: &str, value: &str) -> Result<()> {
    Entry::new(SERVICE, &format!("{kind}:{name}"))
        .context("create keychain entry")?
        .set_password(value)
        .context("write keychain entry")
}

fn get_secret(kind: &str, name: &str) -> Result<Option<String>> {
    let entry = Entry::new(SERVICE, &format!("{kind}:{name}")).context("create keychain entry")?;
    match entry.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(err) => Err(err).context("read keychain entry"),
    }
}

pub fn set_provider_key(provider: &str, value: &str) -> Result<()> {
    set_secret("provider", provider, value)
}

pub fn get_provider_key(provider: &str) -> Result<Option<String>> {
    get_secret("provider", provider)
}

pub fn set_proxy_password(proxy: &str, value: &str) -> Result<()> {
    set_secret("proxy", proxy, value)
}

pub fn get_proxy_password(proxy: &str) -> Result<Option<String>> {
    get_secret("proxy", proxy)
}
