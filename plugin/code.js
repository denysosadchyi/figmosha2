figma.showUI(__html__, { width: 480, height: 520 });

figma.ui.onmessage = async (msg) => {
  if (msg.type !== 'exec') return;

  try {
    // Build async function with Figma API in scope
    const fn = new Function(
      'figma',
      'print',
      `return (async () => { ${msg.code} })();`
    );

    const logs = [];
    const print = (...args) => {
      const text = args.map(a => typeof a === 'object' ? JSON.stringify(a, null, 2) : String(a)).join(' ');
      logs.push(text);
      figma.ui.postMessage({ type: 'log', text });
    };

    const result = await fn(figma, print);

    const output = logs.length > 0
      ? logs.join('\n')
      : result !== undefined
        ? String(result)
        : 'Done';

    figma.ui.postMessage({ type: 'result', text: output });
  } catch (e) {
    figma.ui.postMessage({ type: 'error', text: e.message || String(e) });
  }
};
