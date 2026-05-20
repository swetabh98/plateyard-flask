document.addEventListener('DOMContentLoaded', () => {
    const toggleBtn = document.getElementById('chatbot-toggle-btn');
    const windowEl = document.getElementById('chatbot-window');
    const closeBtn = document.getElementById('chatbot-close');
    const inputEl = document.getElementById('chatbot-input');
    const sendBtn = document.getElementById('chatbot-send');
    const messagesEl = document.getElementById('chatbot-messages');
    const tooltipEl = document.getElementById('yai-tooltip');

    // Toggle Chatbot Window and hide tooltip permanently once opened
    toggleBtn.addEventListener('click', () => {
        if (tooltipEl) tooltipEl.style.opacity = '0'; // Hide tooltip
        
        windowEl.style.display = windowEl.style.display === 'flex' ? 'none' : 'flex';
        if (windowEl.style.display === 'flex') inputEl.focus();
    });

    closeBtn.addEventListener('click', () => windowEl.style.display = 'none');

    function appendMessage(text, sender) {
        const bubble = document.createElement('div');
        bubble.className = `chat-bubble chat-${sender}`;
        bubble.innerHTML = text;
        messagesEl.appendChild(bubble);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    async function sendMessage() {
        const text = inputEl.value.trim();
        if (!text) return;

        appendMessage(text, 'user');
        inputEl.value = '';
        inputEl.disabled = true;
        
        // Show typing indicator
        const typingId = 'typing-' + Date.now();
        const typingBubble = document.createElement('div');
        typingBubble.id = typingId;
        typingBubble.className = 'chat-bubble chat-bot';
        typingBubble.innerHTML = '<em>YAI is analyzing data...</em>';
        messagesEl.appendChild(typingBubble);
        messagesEl.scrollTop = messagesEl.scrollHeight;

        try {
            const res = await fetch('/api/chatbot/ask', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text })
            });
            const result = await res.json();
            
            document.getElementById(typingId).remove();
            appendMessage(result.reply, 'bot');

        } catch (error) {
            document.getElementById(typingId).remove();
            appendMessage("Sorry, I'm having trouble connecting to the database.", 'bot');
        }

        inputEl.disabled = false;
        inputEl.focus();
    }

    sendBtn.addEventListener('click', sendMessage);
    inputEl.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') sendMessage();
    });
});