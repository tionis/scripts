.PHONY: help
help: ## Show this help message
	@grep -E '^[a-zA-Z_./-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

update: ## Update the scripts from this machine's ~/.config/scripts
	find . -type f -executable | xargs -I '{}' -- cp ~/.config/scripts/{} {}
