/* A.S.H.R.A shared chrome: the rail's lane card, driven by /status.
 *
 * Every surface shows the same card, so it must tell the same story: a local
 * model keeps prompts on the machine, a cloud model sends them out. Pages call
 * render() with a /status payload they already have (Home) or load() to fetch
 * one themselves (Galaxy, Brain).
 */
(() => {
  const LOCAL_PROVIDERS = new Set(['ollama', 'lmstudio', 'llama.cpp', 'llamacpp', 'local']);

  const $ = (id) => document.getElementById(id);

  function line(bold, rest) {
    const fragment = document.createDocumentFragment();
    fragment.append(element('b', '', bold), document.createTextNode(rest));
    return fragment;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function render(status) {
    const configured = Boolean(status && status.configured);
    const provider = String((status && status.provider) || '');
    const chatModel = String((status && status.chat_model) || '').toLowerCase();
    const models = Array.isArray(status && status.models) ? status.models : [];
    const chosen = models.find((item) => item.id === chatModel) || null;
    const ready = chosen ? Boolean(chosen.available) : configured;
    const local = chosen
      ? chosen.lane === 'local'
      : configured && LOCAL_PROVIDERS.has(provider.toLowerCase());

    const card = $('lane-card');
    const pulse = $('lane-pulse');
    const title = $('lane-title');
    const copy = $('lane-copy');
    const chip = $('lane-chip');
    const dot = $('lane-dot');

    if (card) {
      card.classList.toggle('cloud', ready && !local);
      card.classList.toggle('off', !ready);
    }

    if (pulse) pulse.className = !ready || !local ? 'pulse amber' : 'pulse';

    if (!ready) {
      if (title) title.textContent = chosen ? `${chosen.label} lane` : 'Model lane';
      if (copy) {
        copy.replaceChildren(
          line(
            'Unavailable',
            ` · ${chosen ? String(chosen.detail || 'not configured') : 'add an API key in config.json'}`
          )
        );
      }
      if (chip) chip.textContent = chosen ? `${chosen.label} unavailable` : 'No model';
      if (dot) dot.className = 'd amber';
      return;
    }

    if (local) {
      if (title) title.textContent = 'Local lane';
      if (copy) copy.replaceChildren(line('Private', ' · nothing leaves the machine'));
      if (chip) chip.textContent = 'Local lane';
      if (dot) dot.className = 'd lime';
      return;
    }

    if (title) title.textContent = 'Cloud lane';
    if (copy) {
      copy.replaceChildren(
        line((chosen && chosen.provider) || provider || 'Provider', ' · prompts leave this machine')
      );
    }
    if (chip) chip.textContent = 'Cloud lane';
    if (dot) dot.className = 'd amber';
  }

  async function load() {
    try {
      const response = await fetch('/status', { headers: { Accept: 'application/json' } });
      if (!response.ok) return;
      render(await response.json());
    } catch {
      /* The rest of the page still works without status. */
    }
  }

  window.ASHRALane = { render, load };
})();
