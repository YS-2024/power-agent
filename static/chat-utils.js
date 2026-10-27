/* Shared request budgeting and keyboard behavior; also tested with Node. */
(function (root) {
  "use strict";
  const utils = {
    shouldSend(event, composing = false) {
      return event.key === "Enter" && !event.shiftKey && !event.altKey && !event.isComposing && !composing && event.keyCode !== 229 && !event.repeat;
    },
    context(history, images = []) {
      const result = history.slice();
      const overBudget = () => {
        const allImages = [...images, ...result.flatMap(item => item.images || [])];
        return result.length > 20 || result.reduce((sum, item) => sum + item.content.length, 0) > 24000 ||
          allImages.length > 8 || allImages.reduce((sum, value) => sum + value.length, 0) > 16000000;
      };
      while (result.length && overBudget()) result.splice(0, 2);
      return result;
    },
    bindSend(input, form, busy) {
      let composing = false;
      input.addEventListener("compositionstart", () => { composing = true; });
      input.addEventListener("compositionend", () => { composing = false; });
      input.addEventListener("keydown", event => {
        if (utils.shouldSend(event, composing)) {
          event.preventDefault();
          if (!busy()) form.requestSubmit();
        }
      });
    },
  };
  if (typeof module !== "undefined" && module.exports) module.exports = utils;
  else root.PowerChat = utils;
})(globalThis);
